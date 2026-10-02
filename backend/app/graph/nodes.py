"""
Graph nodes.

Six nodes, plus `deterministic_reply` as the single escape hatch:

    guard_input -> agent_loop -> verify_reply -> finalize
                                    |            ^
                                    v            |
                        (repair, once)  deterministic_reply

What changed from the previous seventeen-node graph: every branch that used to
be decided in code (location precedence, activity selection, time-window
selection, out-of-scope detection, follow-up reconciliation) is now a tool call
the model makes, validated by `app.agent.schemas`. What remains deterministic is
what must be: sanitisation, the grounding verifier, and the fallback reply.
"""
import logging
from typing import Any, Dict, List

from app import guardrails
from app.agent.loop import grounding_problems, run_agent
from app.agent.schemas import TERMINAL_OUTCOMES

# Outcomes a node has already decided. The deterministic fallback reports
# `llm_unavailable` and `no_sop` itself; finalize must not overwrite those with
# a guess just because they are not in the model's end_turn enum.
SETTLED_OUTCOMES = frozenset(TERMINAL_OUTCOMES) | {"llm_unavailable", "unavailable"}
from app.agent.tools import registry_from_state
from app.engine.policies import evaluate, is_valid_activity
from app.engine.verifier import verify_reply
from app.llm.client import complete_text
from app.llm.prompts import (
    build_advisory_system_prompt,
)
from app.graph.state import GraphState

logger = logging.getLogger(__name__)

# Preloaded once; SOP files do not change at runtime.
from app.tools.sop_loader import load_sops  # noqa: E402  (after config import)

SOP_REGISTRY = load_sops()

# Loaded up front so a malformed reply never turns into a policy fact.
REPAIR_INSTRUCTION = (
    "Your previous reply failed verification. Produce a corrected reply using "
    "only the numbers and policy ids already in this conversation, and cite at "
    "least one policy id."
)


def _append_trace(state: GraphState, *entries: str) -> List[str]:
    return list(state.get("trace") or []) + list(entries)


def _session_context(state: GraphState) -> Dict[str, Any]:
    location = state.get("location")
    return {
        "location_label": location.label if location else None,
        "activity": state.get("activity"),
        "window": state.get("time_window"),
    }


# --- 1. Guard ---

def guard_input(state: GraphState) -> Dict[str, Any]:
    """
    Sanitise the message and screen it for prompt injection.

    Patterns and redactions come from app/guardrails/patterns.yaml. A flagged
    message is still sanitised and traced, but never reaches the model as a
    request.
    """
    sanitized, injection_flag = guardrails.sanitize_message(state.get("message", ""))
    return {
        "sanitized_message": sanitized,
        "injection_flag": injection_flag,
        "trace": _append_trace(state, "guard_input"),
    }


# --- 2. Agent loop ---

def agent_loop(state: GraphState) -> Dict[str, Any]:
    """
    Let the model resolve the location, window and activity through tools, then
    answer from the resulting policy evaluation.

    Returns engine output into state so the verifier, the API response and the
    fallback all read the same values.
    """
    ctx = registry_from_state(state, SOP_REGISTRY)
    system_prompt = build_advisory_system_prompt(
        taxonomy=SOP_REGISTRY.taxonomy,
        sop_count=len(SOP_REGISTRY.sops),
        injection_flagged=bool(state.get("injection_flag")),
        session_context=_session_context(state),
    )

    outcome = run_agent(
        system_prompt=system_prompt,
        user_message=state.get("sanitized_message", ""),
        ctx=ctx,
    )

    updates: Dict[str, Any] = dict(outcome.updates)
    updates["trace"] = _append_trace(state, "agent_loop", *outcome.trace)

    if outcome.unreachable:
        # No model, no paraphrase. The deterministic path takes over.
        updates["error_message"] = outcome.error or "LLM unavailable"
        updates["outcome"] = "llm_unavailable"
        return updates

    if outcome.reply:
        updates["reply"] = outcome.reply
    if outcome.outcome:
        updates.setdefault("outcome", outcome.outcome)
    if outcome.exhausted:
        updates["error_message"] = "Agent exhausted its step budget."

    return updates


# --- 3. Verify ---

def verify_reply_node(state: GraphState) -> Dict[str, Any]:
    """
    Gate the reply on the deterministic engine's output.

    Checks every number against the derived facts and matched SOP text, and
    every cited policy id against the policies that actually matched.
    """
    reply = state.get("reply", "")
    facts = state.get("facts") or {}
    matched = [item[0] for item in (state.get("matched_sops") or [])]
    ranking = state.get("ranking_result") or {}
    allowed_ids = ranking.get("all_sop_ids") or []
    combined_advice = ranking.get("combined_advice") or ""

    is_valid, errors = verify_reply(
        reply=reply,
        facts=facts,
        matched_sops=matched,
        allowed_sop_ids=allowed_ids,
        rendered_advice=[combined_advice] if combined_advice else [],
    )

    if is_valid:
        ctx = registry_from_state(state, SOP_REGISTRY)
        errors = grounding_problems(reply, ctx, state)

    return {
        "verify_passed": is_valid and not errors,
        "verify_retries": int(state.get("verify_retries", 0)) + 1,
        "verify_errors": errors,
        "trace": _append_trace(state, "verify_reply"),
    }


# --- 4. Repair ---

def repair_reply(state: GraphState) -> Dict[str, Any]:
    """
    One re-prompt when verification fails, with the failure reasons attached.

    Bounded by `verify_repair_attempts` in policy config; past that the graph
    drops to the deterministic reply rather than looping.
    """
    errors = state.get("verify_errors") or []
    reply = state.get("reply", "")

    corrected = complete_text(
        prompt=(
            f"{REPAIR_INSTRUCTION}\n\n"
            f"Problems found:\n- " + "\n- ".join(errors) + "\n\n"
            f"Your previous reply:\n{reply}\n\n"
            "Write the corrected reply only."
        ),
        system_prompt=build_advisory_system_prompt(
                taxonomy=sorted(SOP_REGISTRY.taxonomy),
                sop_count=len(SOP_REGISTRY.sops),
            ),
    )

    return {
        "reply": corrected or reply,
        "trace": _append_trace(state, "repair_reply"),
    }


# --- 5. Deterministic fallback ---

def _unknown_activity_reply(activity: str) -> str:
    """
    Say plainly that nothing is published for this activity, and list what is.

    Better than the offline template: the user gets the taxonomy back, which
    lets the next turn succeed even though this one had no model.
    """
    covered = ", ".join(sorted(SOP_REGISTRY.taxonomy))
    return guardrails.template(
        "no_policy_match_for_activity", activity=activity, covered=covered
    )


def deterministic_reply(state: GraphState) -> Dict[str, Any]:
    """
    The un-rephraseable path.

    Runs on when the model is unreachable, when verification keeps failing, or
    when a test injects a forecast directly. Produces the published advice
    verbatim from the ranking engine, which is the answer the project promises
    even with no LLM in the loop.
    """
    facts = state.get("facts") or {}
    activity = state.get("activity")

    ranking = state.get("ranking_result") or {}
    if not ranking:
        # Engine never ran (LLM was down before any tool call). Run it here.
        raw_weather = state.get("raw_weather")

        if not raw_weather or not activity:
            return {
                "reply": guardrails.template("offline"),
                "outcome": "llm_unavailable",
                "trace": _append_trace(state, "deterministic_reply"),
            }

        if not is_valid_activity(activity, SOP_REGISTRY):
            # We have a forecast but the activity is outside the published
            # taxonomy. That is an honest no_sop, not a model failure: no
            # guidance exists for chess, whatever the model is doing.
            return {
                "reply": _unknown_activity_reply(activity),
                "outcome": "no_sop",
                "facts": {},
                "trace": _append_trace(state, "deterministic_reply"),
            }

        from app.engine.facts import derive_facts

        facts = derive_facts(raw_weather, time_window=state.get("time_window") or "today")
        evaluation = evaluate(SOP_REGISTRY, activity, facts)
        ranking = evaluation.ranking
        if not evaluation:
            location = state.get("location")
            return {
                "reply": guardrails.template(
                    "no_policy_match",
                    location=location.label if location else "that location",
                ),
                "outcome": "no_sop",
                "facts": facts,
                "matched_sops": evaluation.legacy_matched,
                "ranking_result": ranking,
                "trace": _append_trace(state, "deterministic_reply"),
            }

    location = state.get("location")
    location_label = location.label if location else "your location"

    if not ranking.get("all_sop_ids"):
        return {
            "reply": guardrails.template(
                "no_policy_match", location=location_label
            ),
            "outcome": "no_sop",
            "trace": _append_trace(state, "deterministic_reply"),
        }

    reply = guardrails.template(
        "deterministic_reply",
        location=location_label,
        advice=ranking.get("combined_advice", ""),
        note=state.get("location_note", ""),
    ).strip()

    return {
        "reply": reply,
        "outcome": "answered",
        "facts": facts,
        "matched_sops": state.get("matched_sops") or [],
        "ranking_result": ranking,
        "trace": _append_trace(state, "deterministic_reply"),
    }


# --- 6. Finalize ---

def finalize(state: GraphState) -> Dict[str, Any]:
    """
    Settle the outcome and leave the session state the next turn will reuse.

    The model normally reports the outcome itself via `end_turn`. This node only
    fills in the gaps: a reply that never materialised is `unavailable`, and the
    deterministic fallback's own outcome wins over anything the model guessed.
    """
    outcome = state.get("outcome")

    if outcome not in SETTLED_OUTCOMES:
        ranking = state.get("ranking_result") or {}
        if state.get("reply") and ranking.get("all_sop_ids"):
            outcome = "answered"
        elif state.get("reply"):
            outcome = "clarify"
        elif state.get("error_message"):
            outcome = "unavailable"
        else:
            outcome = "unavailable"

    if not state.get("reply"):
        outcome = "unavailable"
        reply = guardrails.template("unavailable")
    else:
        reply = state["reply"]

    result: Dict[str, Any] = {
        "reply": reply,
        "outcome": outcome,
        "trace": _append_trace(state, "finalize"),
    }

    # Carried over verbatim, which is what lets "and tomorrow?" resolve without
    # re-asking the city.
    location = state.get("location")
    if location:
        result["location"] = location
    if state.get("activity"):
        result["activity"] = state["activity"]
    if state.get("time_window"):
        result["time_window"] = state["time_window"]

    return result