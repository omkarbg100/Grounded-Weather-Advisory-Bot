from typing import Dict, Any, List, Optional, Tuple, Union
from app.schemas import SOPModel, SOPRegistry, WhenGroup, SingleCondition


def eval_condition(cond: Union[SingleCondition, WhenGroup, Dict[str, Any]], facts: Dict[str, Any]) -> bool:
    if isinstance(cond, dict):
        if "all" in cond and cond["all"]:
            return all(eval_condition(item, facts) for item in cond["all"])
        if "any" in cond and cond["any"]:
            return any(eval_condition(item, facts) for item in cond["any"])
        if "fact" in cond:
            fact_name = cond["fact"]
            op = cond["op"]
            target = cond["value"]
            return _eval_op(facts.get(fact_name), op, target)
        return False

    if isinstance(cond, SingleCondition):
        fact_val = facts.get(cond.fact)
        return _eval_op(fact_val, cond.op, cond.value)

    if isinstance(cond, WhenGroup):
        if cond.all:
            if not all(eval_condition(item, facts) for item in cond.all):
                return False
        if cond.any:
            if not any(eval_condition(item, facts) for item in cond.any):
                return False
        return True

    return False


def _eval_op(fact_val: Any, op: str, target_val: Any) -> bool:
    if fact_val is None:
        return False

    try:
        if op == ">":
            return float(fact_val) > float(target_val)
        elif op == ">=":
            return float(fact_val) >= float(target_val)
        elif op == "<":
            return float(fact_val) < float(target_val)
        elif op == "<=":
            return float(fact_val) <= float(target_val)
        elif op == "==":
            return float(fact_val) == float(target_val)
        elif op == "between":
            if isinstance(target_val, (list, tuple)) and len(target_val) == 2:
                low, high = float(target_val[0]), float(target_val[1])
                return low <= float(fact_val) <= high
            return False
        elif op == "in":
            if isinstance(target_val, (list, tuple, set)):
                return fact_val in target_val or int(fact_val) in target_val
            return False
    except (ValueError, TypeError):
        return False

    return False


def match_sops(
    registry: SOPRegistry,
    activity: str,
    facts: Dict[str, Any]
) -> List[Tuple[SOPModel, str]]:
    """
    Evaluates registry SOPs against the user activity and derived facts.
    Returns list of tuples: (matched_sop, rendered_advice_text).
    """
    matched: List[Tuple[SOPModel, str]] = []
    norm_activity = activity.lower().strip() if activity else "unknown"

    for sop in registry.sops:
        # Check activity eligibility
        activity_matches = (
            "*" in sop.applies_to
            or norm_activity in [a.lower() for a in sop.applies_to]
        )
        if not activity_matches:
            continue

        # Deterministic logic SOPs (using 'when')
        if sop.when:
            if eval_condition(sop.when, facts):
                advice_text = render_advice_template(sop.advice or "", facts)
                matched.append((sop, advice_text))

        # Fuzzy scoring SOPs (using 'scoring')
        elif sop.scoring:
            total_score = 0.0
            total_weight = 0.0

            for criterion in sop.scoring.criteria:
                total_weight += criterion.weight
                fact_val = facts.get(criterion.fact)
                if _eval_op(fact_val, criterion.op, criterion.value):
                    total_score += criterion.weight

            norm_score = total_score / total_weight if total_weight > 0 else 0.0

            # Find matching band (sorted descending by min_score)
            sorted_bands = sorted(sop.scoring.bands, key=lambda b: b.min_score, reverse=True)
            matched_band = None
            for band in sorted_bands:
                if norm_score >= band.min_score:
                    matched_band = band
                    break

            if matched_band:
                advice_text = render_advice_template(matched_band.advice, facts)
                matched.append((sop, advice_text))

    return matched


def render_advice_template(template: str, facts: Dict[str, Any]) -> str:
    """
    Replaces {fact_name} placeholders in template string with actual numeric values from facts.
    """
    rendered = template
    for key, val in facts.items():
        placeholder = f"{{{key}}}"
        if placeholder in rendered:
            rendered = rendered.replace(placeholder, str(val))
    return rendered


# --- Explainability ---
#
# `match_sops` answers "did this SOP match?". The helpers below answer "why?",
# so the tool layer can hand the model a justification derived from the same
# rule evaluation that produced the match.


def describe_condition(condition: Any, facts: Dict[str, Any]) -> Optional[str]:
    """
    Render one condition as `wind_kmh >= 35 (actual 36.4)`, or None if the
    fact is missing and the condition could not be evaluated.
    """
    fact_name = getattr(condition, "fact", None) or (
        condition.get("fact") if isinstance(condition, dict) else None
    )
    if fact_name is None:
        return None

    op = getattr(condition, "op", None) or (
        condition.get("op") if isinstance(condition, dict) else None
    )
    target = getattr(condition, "value", None)
    if target is None and isinstance(condition, dict):
        target = condition.get("value")

    actual = facts.get(fact_name)
    if actual is None:
        return f"{fact_name} {op} {_format_target(target)} (no data)"
    return f"{fact_name} {op} {_format_target(target)} (actual {_format_value(actual)})"


def _format_target(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_format_value(v) for v in value) + "]"
    return _format_value(value)


def _format_value(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _walk_conditions(node: Any, facts: Dict[str, Any], sink: List[str]) -> None:
    """Flatten a WhenGroup / SingleCondition / dict tree into leaf conditions."""
    if node is None:
        return

    if hasattr(node, "all") or (isinstance(node, dict) and node.get("all")):
        children = node.all if hasattr(node, "all") else node["all"]
        for child in children or []:
            _walk_conditions(child, facts, sink)
        return

    if hasattr(node, "any") or (isinstance(node, dict) and node.get("any")):
        children = node.any if hasattr(node, "any") else node["any"]
        for child in children or []:
            _walk_conditions(child, facts, sink)
        return

    rendered = describe_condition(node, facts)
    if rendered:
        sink.append(rendered)


def explain_sop(sop: SOPModel, facts: Dict[str, Any]) -> List[str]:
    """
    Every condition text for a SOP, regardless of whether it evaluated true.

    A `when` SOP lists its `all`/`any` predicates; a `scoring` SOP lists the
    weighted criteria that contributed to its score.
    """
    reasons: List[str] = []
    if sop.when:
        _walk_conditions(sop.when, facts, reasons)
    elif sop.scoring:
        for criterion in sop.scoring.criteria:
            rendered = describe_condition(criterion, facts)
            if rendered:
                reasons.append(f"{rendered} [weight {criterion.weight}]")
    return reasons


def score_sop(sop: SOPModel, facts: Dict[str, Any]) -> Optional[float]:
    """
    Normalised fuzzy score for a scoring SOP, or None if the SOP is not a
    scoring SOP or has no criteria.
    """
    if not sop.scoring:
        return None
    total_score = 0.0
    total_weight = 0.0
    for criterion in sop.scoring.criteria:
        total_weight += criterion.weight
        if _eval_op(facts.get(criterion.fact), criterion.op, criterion.value):
            total_score += criterion.weight
    if total_weight <= 0:
        return 0.0
    return round(total_score / total_weight, 4)
