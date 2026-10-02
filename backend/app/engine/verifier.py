import re
from typing import Dict, Any, List, Set, Tuple
from app.schemas import SOPModel

NUMBER_REGEX = re.compile(r"(?<![A-Za-z0-9_])[-+]?\d+(?:\.\d+)?\b")
SOP_ID_REGEX = re.compile(r"\b[A-Z]{3}-[A-Z0-9-]+(?:\d+)?\b")


def extract_numbers_from_text(text: str) -> List[float]:
    # Strip SOP IDs first so hyphenated suffixes like -01 are not parsed as negative numbers
    text_without_ids = SOP_ID_REGEX.sub("", text)
    matches = NUMBER_REGEX.findall(text_without_ids)
    nums = []
    for m in matches:
        try:
            nums.append(float(m))
        except ValueError:
            pass
    return nums


def collect_allowed_numbers(
    facts: Dict[str, Any],
    matched_sops: List[SOPModel]
) -> Set[float]:
    allowed: Set[float] = set()

    # Add facts values
    for val in facts.values():
        if isinstance(val, (int, float)):
            allowed.add(round(float(val), 1))
            allowed.add(float(int(val)))

    # Add numbers from matched SOP thresholds and advice
    for sop in matched_sops:
        if sop.advice:
            for num in extract_numbers_from_text(sop.advice):
                allowed.add(round(num, 1))

        if sop.when:
            _collect_from_when(sop.when, allowed)

        if sop.scoring:
            for criterion in sop.scoring.criteria:
                _add_val(criterion.value, allowed)
            for band in sop.scoring.bands:
                allowed.add(round(band.min_score, 1))
                for num in extract_numbers_from_text(band.advice):
                    allowed.add(round(num, 1))

    # Add standard time/date/constant tokens (e.g., 24, 7, 50, 10, 16)
    allowed.update([24.0, 7.0, 50.0, 10.0, 16.0, 0.0, 1.0, 2.0, 3.0, 50.0])

    return allowed


def _collect_from_when(when_group, allowed_set: Set[float]):
    if when_group.all:
        for item in when_group.all:
            _collect_from_item(item, allowed_set)
    if when_group.any:
        for item in when_group.any:
            _collect_from_item(item, allowed_set)


def _collect_from_item(item, allowed_set: Set[float]):
    if hasattr(item, "value"):
        _add_val(item.value, allowed_set)
    elif isinstance(item, dict):
        if "value" in item:
            _add_val(item["value"], allowed_set)
        if "all" in item:
            for sub in item["all"]:
                _collect_from_item(sub, allowed_set)
        if "any" in item:
            for sub in item["any"]:
                _collect_from_item(sub, allowed_set)


def _add_val(val: Any, allowed_set: Set[float]):
    if isinstance(val, (int, float)):
        allowed_set.add(round(float(val), 1))
    elif isinstance(val, (list, tuple)):
        for v in val:
            if isinstance(v, (int, float)):
                allowed_set.add(round(float(v), 1))


def verify_reply(
    reply: str,
    facts: Dict[str, Any],
    matched_sops: List[SOPModel],
    allowed_sop_ids: List[str]
) -> Tuple[bool, List[str]]:
    """
    Verifies that:
    1. Every number in reply appears in the allowed set (fact values, SOP thresholds, time tokens).
    2. Every cited SOP ID in reply is present in allowed_sop_ids.

    Returns (is_valid, list_of_error_strings).
    """
    errors: List[str] = []
    allowed_numbers = collect_allowed_numbers(facts, matched_sops)
    reply_numbers = extract_numbers_from_text(reply)

    for num in reply_numbers:
        rounded_num = round(num, 1)
        # Allow exact or rounded match
        if rounded_num not in allowed_numbers and float(int(num)) not in allowed_numbers:
            errors.append(f"Unallowed numeric value in reply: {num}")

    # Check SOP ID citations
    cited_ids = SOP_ID_REGEX.findall(reply)
    for cited in cited_ids:
        if cited not in allowed_sop_ids:
            errors.append(f"Cited SOP ID '{cited}' is not among matched SOP IDs: {allowed_sop_ids}")

    return len(errors) == 0, errors
