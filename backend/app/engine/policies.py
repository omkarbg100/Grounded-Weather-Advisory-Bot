"""
Deterministic policy evaluation.

This is the boundary the LLM is not allowed to cross. The model may choose an
activity tag and a forecast window; it cannot author, rank, or select policy.
Given a forecast's facts, this module runs the YAML rule engine, ranks the
matches, and returns both the advice and the justification for each match.

The same function backs the `evaluate_policies` tool and the offline fallback
path, so both paths are guaranteed to agree.
"""
from typing import Any, Dict, List, Optional, Tuple

from app.schemas import SOPModel, SOPRegistry
from app.engine.matcher import match_sops, explain_sop, score_sop
from app.engine.ranking import resolve_conflicts

UNKNOWN_ACTIVITY = "unknown"


class PolicyEvaluation:
    """
    Outcome of evaluating the SOP registry against one activity and fact set.

    Attributes:
        activity: the activity tag evaluated.
        matched: every matching SOP with its rendered advice and explanation.
        primary: the top-ranked SOP id.
        secondaries: ids of the additional SOPs carried into the reply.
        all_sop_ids: every matching id, rank ordered.
        combined_advice: rendered primary + secondary advice blocks.
        facts: the fact set the evaluation ran against.
    """

    __slots__ = (
        "activity",
        "matched",
        "primary",
        "secondaries",
        "all_sop_ids",
        "combined_advice",
        "facts",
        "ranking",
        "legacy_matched",
    )

    def __init__(
        self,
        activity: str,
        matched: List[Dict[str, Any]],
        ranking: Dict[str, Any],
        facts: Dict[str, Any],
        legacy_matched: List[Tuple[SOPModel, str]],
    ):
        self.activity = activity
        self.matched = matched
        self.ranking = ranking
        self.facts = facts
        self.legacy_matched = legacy_matched
        self.primary = ranking.get("primary")
        self.secondaries = [item[0].id for item in (ranking.get("secondaries") or [])]
        self.all_sop_ids = ranking.get("all_sop_ids") or []
        self.combined_advice = ranking.get("combined_advice") or ""

    def __bool__(self) -> bool:
        return bool(self.all_sop_ids)

    def as_policy_dicts(self) -> List[Dict[str, Any]]:
        return list(self.matched)


def normalise_activity(activity: Optional[str]) -> str:
    if not activity:
        return UNKNOWN_ACTIVITY
    return str(activity).strip().lower() or UNKNOWN_ACTIVITY


def is_valid_activity(activity: Optional[str], registry: SOPRegistry) -> bool:
    """
    Whether a tag the model supplied is one the SOP registry can act on.

    `unknown` is allowed: SOPs with `applies_to: ["*"]` still apply and the
    matcher decides, rather than us silently rewriting the model's choice.
    """
    normalised = normalise_activity(activity)
    return normalised == UNKNOWN_ACTIVITY or normalised in {
        tag.lower() for tag in registry.taxonomy
    }


def evaluate(
    registry: SOPRegistry,
    activity: Optional[str],
    facts: Dict[str, Any],
    include_ids: Optional[List[str]] = None,
) -> PolicyEvaluation:
    """
    Evaluate every SOP against `activity` and `facts`, then rank the matches.

    `include_ids`, when given, narrows the *reported* matches to those ids
    (after ranking, so ordering is unaffected). It cannot introduce a SOP that
    the rule engine did not match.
    """
    normalised = normalise_activity(activity)
    legacy_matched: List[Tuple[SOPModel, str]] = match_sops(registry, normalised, facts)
    ranking = resolve_conflicts(legacy_matched)

    allow = {str(pid).strip().upper() for pid in include_ids} if include_ids else None

    explained: List[Dict[str, Any]] = []
    for sop, advice in legacy_matched:
        if allow is not None and sop.id.upper() not in allow:
            continue
        explained.append(
            {
                "id": sop.id,
                "title": sop.title,
                "category": sop.category,
                "severity": sop.severity,
                "precedence": sop.precedence,
                "advice": advice,
                "why": explain_sop(sop, facts),
                "score": score_sop(sop, facts),
                "tags": list(sop.tags or []),
            }
        )

    # Preserve the rank order the ranking engine produced.
    order = {pid: index for index, pid in enumerate(ranking.get("all_sop_ids") or [])}
    explained.sort(key=lambda item: order.get(item["id"], len(order)))

    return PolicyEvaluation(
        activity=normalised,
        matched=explained,
        ranking=ranking,
        facts=dict(facts),
        legacy_matched=legacy_matched,
    )


def to_tool_payload(evaluation: PolicyEvaluation, include_facts: bool = True) -> Dict[str, Any]:
    """
    Serialise an evaluation for the model.

    Only matched policy text is included. Unmatched SOPs are not shown: letting
    the model see near-misses invites it to reason about policies that do not
    apply.
    """
    payload: Dict[str, Any] = {
        "activity": evaluation.activity,
        "matched": evaluation.matched,
        "primary_sop_id": evaluation.primary[0].id if evaluation.primary else None,
        "secondary_sop_ids": evaluation.secondaries,
        "all_sop_ids": evaluation.all_sop_ids,
        "combined_advice": evaluation.combined_advice,
    }
    if include_facts:
        payload["facts"] = evaluation.facts
    return payload