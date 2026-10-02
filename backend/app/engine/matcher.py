from typing import Dict, Any, List, Tuple, Union
from app.schemas import SOPModel, SOPRegistry, WhenGroup, SingleCondition, FuzzyScoring


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
