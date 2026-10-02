"""
The reasoning loop.

Drives the model through the tools until it produces an answer or runs out of
budget. The loop owns only sequencing: what the model may influence is limited
to tool arguments, and every tool argument is validated before use.

Termination, in order of precedence:
  1. The model returns text with no tool calls.
  2. `max_steps` is reached, in which case the model gets one final turn with an
     explicit note that the budget is spent.
  3. The model is unreachable, which raises LLMUnavailable so the graph can take
     the deterministic fallback.

The loop also enforces a grounding rule a plain ReAct loop does not: safety
guidance must trace back to an `evaluate_policies` result.
"""
import logging
from typing import Any, Dict, List, Optional, Tuple

from app import policy_config
from app.agent.schemas import ToolResult
from app.agent.tools import (
    EndTurnSignal,
    ToolContext,
    build_gemini_tools,
    build_tools,
    dispatch,
)
from app.engine.verifier import requires_policy_citation
from app.llm.client import (
    LLMUnavailable,
    append_function_responses,
    append_model_turn,
    run_tool_turn,
    user_turn,
)

logger = logging.getLogger(__name__)

POLICY_TOOL = "evaluate_policies"


class AgentOutcome:
    """
    Result of a loop run.

    Attributes:
        reply: the reply to send, or "" if the model never produced one.
        outcome: the terminal classification the model reported via end_turn.
        updates: state keys produced by tools, merged by the caller.
        trace: one entry per tool call.
        exhausted: True when the step budget ran out mid-task.
        unreachable: True when the model could not be called at all.
        called_policies: True when evaluate_policies ran at least once.
    """

    __slots__ = (
        "reply",
        "outcome",
        "updates",
        "trace",
        "exhausted",
        "unreachable",
        "steps",
        "called_policies",
        "error",
    )

    def __init__(
        self,
        reply: str = "",
        outcome: str = "",
        updates: Optional[Dict[str, Any]] = None,
        trace: Optional[List[str]] = None,
        exhausted: bool = False,
        unreachable: bool = False,
        steps: int = 0,
        called_policies: bool = False,
        error: str = "",
    ):
        self.reply = reply
        self.outcome = outcome
        self.updates = dict(updates or {})
        self.trace = list(trace or [])
        self.exhausted = exhausted
        self.unreachable = unreachable
        self.steps = steps
        self.called_policies = called_policies
        self.error = error

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"AgentOutcome(steps={self.steps}, exhausted={self.exhausted}, "
            f"unreachable={self.unreachable}, called_policies={self.called_policies})"
        )


def run_agent(
    system_prompt: str,
    user_message: str,
    ctx: ToolContext,
    max_steps: Optional[int] = None,
    forbid_tools: bool = False,
) -> AgentOutcome:
    """
    Run the tool loop to completion.

    Args:
        system_prompt: the advisory system prompt.
        user_message: the sanitised user message.
        ctx: tool context, holding refs carried over from earlier turns.
        max_steps: overrides the configured budget.
        forbid_tools: answer from the prompt alone, used for greetings and
            out-of-scope messages where no tool call is appropriate.
    """
    outcome = _run_loop(
        system_prompt=system_prompt,
        user_message=user_message,
        ctx=ctx,
        max_steps=max_steps,
        forbid_tools=forbid_tools,
    )

    # Refs minted during the run become part of state, so verify_reply and
    # repair_reply can tell that a forecast was actually retrieved rather than
    # trusting the reply's word for it.
    outcome.updates.setdefault("location_refs", dict(ctx.locations))
    outcome.updates.setdefault("forecast_refs", dict(ctx.forecasts))
    outcome.updates.setdefault("session_location", ctx.session_location)
    return outcome


def _run_loop(
    system_prompt: str,
    user_message: str,
    ctx: ToolContext,
    max_steps: Optional[int] = None,
    forbid_tools: bool = False,
) -> AgentOutcome:
    agent_config = policy_config.agent_config()
    budget = max(1, int(max_steps or agent_config.get("max_steps", 6)))
    history_limit = max(4, int(agent_config.get("max_history_messages", 24)))

    history: List[Any] = [user_turn(user_message)]
    tools = None if forbid_tools else build_gemini_tools()
    registry = {tool.name: tool for tool in build_tools()}

    updates: Dict[str, Any] = {}
    trace: List[str] = []
    called_policies = False

    for step in range(1, budget + 1):
        try:
            turn = run_tool_turn(system_prompt, history, tools=tools)
        except LLMUnavailable as exc:
            logger.warning("[Agent] Model unreachable: %s", exc)
            return AgentOutcome(
                updates=updates,
                trace=trace,
                unreachable=True,
                steps=step - 1,
                called_policies=called_policies,
                error=str(exc),
            )

        append_model_turn(history, turn)

        if not turn.wants_tools:
            _trim(history, history_limit)
            return AgentOutcome(
                reply=turn.text,
                updates=updates,
                trace=trace,
                steps=step,
                called_policies=called_policies,
            )

        responses: List[Tuple[str, Dict[str, Any]]] = []
        stop: Optional[EndTurnSignal] = None

        for name, raw_args in turn.function_calls:
            tool = registry.get(name)
            if tool is None:
                result: ToolResult = ToolResult.fail(
                    f"Unknown tool '{name}'.",
                    available_tools=sorted(registry),
                )
            else:
                try:
                    result = dispatch(tool, raw_args, ctx)
                except EndTurnSignal as signal:
                    stop = signal
                    break
                trace.append(_trace_entry(name, raw_args, result))
                if name == POLICY_TOOL and not result.is_error:
                    called_policies = True

            updates.update(result.updates)
            responses.append((name, result.payload))

        if stop is not None:
            updates.update(stop.updates)
            trace.append(f"tool:end_turn(outcome={stop.outcome})->ok")
            _trim(history, history_limit)
            return AgentOutcome(
                reply=stop.reply,
                updates=updates,
                trace=trace,
                steps=step,
                called_policies=called_policies,
                outcome=stop.outcome,
            )

        append_function_responses(history, responses)

        if step == budget:
            # One final turn, told the budget is spent, so the user gets an
            # answer rather than a truncated conversation.
            append_function_responses(
                history,
                [
                    (
                        POLICY_TOOL,
                        {
                            "note": (
                                "Step budget exhausted. Answer now using only the "
                                "results already retrieved, or say what is missing."
                            )
                        },
                    )
                ],
            )
            try:
                final = run_tool_turn(system_prompt, history, tools=None)
            except LLMUnavailable as exc:
                return AgentOutcome(
                    updates=updates,
                    trace=trace,
                    unreachable=True,
                    steps=step,
                    called_policies=called_policies,
                    error=str(exc),
                )
            _trim(history, history_limit)
            return AgentOutcome(
                reply=final.text,
                updates=updates,
                trace=trace,
                exhausted=final.wants_tools,
                steps=step + 1,
                called_policies=called_policies,
            )

    return AgentOutcome(
        updates=updates,
        trace=trace,
        exhausted=True,
        steps=budget,
        called_policies=called_policies,
    )


def _trace_entry(name: str, raw_args: Dict[str, Any], result: ToolResult) -> str:
    """Compact trace line. Arguments are summarised, never dumped wholesale."""
    summary = ", ".join(
        f"{key}={_abbreviate(value)}" for key, value in list(raw_args.items())[:3]
    )
    return f"tool:{name}({summary})->{'error' if result.is_error else 'ok'}"


def _abbreviate(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= 40 else text[:37] + "..."


def _trim(history: List[Any], limit: int) -> None:
    """Keep the conversation bounded, dropping the oldest turns first."""
    overflow = len(history) - limit
    if overflow <= 1:
        return
    del history[1 : 1 + overflow]


def grounding_problems(
    reply: str,
    ctx: ToolContext,
    state: Dict[str, Any],
) -> List[str]:
    """
    Reasons the reply is not safe to send yet. Empty means it is fine.

    Checks what the model cannot be trusted to self-enforce: a safety answer
    needs a retrieved forecast behind it, and when policies matched, the reply
    must cite at least one of them.
    """
    ranking = state.get("ranking_result") or {}
    matched_ids = ranking.get("all_sop_ids") or []

    # Nothing was investigated: not this function's problem to judge.
    if not ctx.forecasts and not matched_ids:
        return []

    problems: List[str] = []

    if not ctx.forecasts:
        problems.append(
            "No forecast was retrieved. Call get_forecast before answering any "
            "safety question."
        )
        return problems

    if not reply.strip():
        problems.append("The reply is empty.")
        return problems

    if matched_ids and requires_policy_citation(reply, matched_ids):
        problems.append(
            f"The reply cites no policy id. Cite at least one of {matched_ids}."
        )

    return problems