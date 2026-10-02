"""
Grounding verification for the agent's reply.

The agent may only state numbers that the deterministic engine actually
produced. This module extracts every number and SOP id from the reply and
checks it against an allowlist built from two sources:

1. the derived facts, and
2. the full text of the matched SOPs -- every threshold, every `{fact}`
   template value, and every literal number the advice prose contains.

The previous implementation appended a blanket list
(`[24, 7, 50, 10, 16, 0, 1, 2, 3]`) to keep common time-of-day and sunscreen
mentions from tripping the check. That list was a hardcode which also let
unrelated numbers through; now clock times and similar constants are harvested
from the SOP text that is actually being cited.
"""
import re
from typing import Dict, Any, Iterable, List, Set

from app.schemas import SOPModel

NUMBER_REGEX = re.compile(r"(?<![A-Za-z0-9_])[-+]?\d+(?:\.\d+)?\b")
SOP_ID_REGEX = re.compile(r"\b[A-Z]{3}-[A-Z0-9-]+(?:\d+)?\b")

# Formats that introduce a legitimate constant rather than a weather reading.
_CONSTANT_PATTERNS = (
    re.compile(r"\b\d{1,2}:\d{2}\b"),                  # clock times: 10:00, 16:00
    re.compile(r"\bSPF\s*\d+\b", re.IGNORECASE),      # sunscreen grades
    re.compile(r"\b\d+\s*-\s*\d+\s*hours?\b", re.IGNORECASE),
)


def extract_numbers_from_text(text: str) -> List[float]:
    """
    Every number in `text`, with SOP ids and clock times stripped first so that
    `EXE-WIND-01` and `10:00` do not register as -1 and 10.
    """
    cleaned = SOP_ID_REGEX.sub("", text or "")
    for pattern in _CONSTANT_PATTERNS:
        cleaned = pattern.sub(" ", cleaned)

    numbers: List[float] = []
    for match in NUMBER_REGEX.findall(cleaned):
        try:
            numbers.append(float(match))
        except ValueError:
            continue
    return numbers


def _add_value(value: Any, allowed: Set[float]) -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        allowed.add(round(float(value), 1))
        allowed.add(float(int(value)))
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            _add_value(item, allowed)


def _collect_from_when(when_group: Any, allowed: Set[float]) -> None:
    if when_group is None:
        return
    if getattr(when_group, "all", None):
        for item in when_group.all:
            _collect_from_when(item, allowed)
    if getattr(when_group, "any", None):
        for item in when_group.any:
            _collect_from_when(item, allowed)
    _add_value(getattr(when_group, "value", None), allowed)


def collect_allowed_numbers(
    facts: Dict[str, Any],
    matched_sops: Iterable[SOPModel],
    rendered_advice: Iterable[str] = (),
) -> Set[float]:
    """
    Build the allowlist of numbers a reply is permitted to state.

    Sources: the derived facts, every threshold in the matched SOP conditions,
    every score band, and every literal number appearing in the SOP advice
    prose (both the raw template and its rendered form, which is what the model
    actually receives).
    """
    allowed: Set[float] = set()

    for value in (facts or {}).values():
        _add_value(value, allowed)

    for advice in rendered_advice:
        for number in extract_numbers_from_text(advice or ""):
            allowed.add(round(number, 1))
            allowed.add(float(int(number)))

    for sop in matched_sops or []:
        if sop.advice:
            for number in extract_numbers_from_text(sop.advice):
                allowed.add(round(number, 1))
                allowed.add(float(int(number)))

        _collect_from_when(sop.when, allowed)

        if sop.scoring:
            for criterion in sop.scoring.criteria:
                _add_value(criterion.value, allowed)
            for band in sop.scoring.bands:
                allowed.add(round(band.min_score, 1))
                for number in extract_numbers_from_text(band.advice):
                    allowed.add(round(number, 1))
                    allowed.add(float(int(number)))

    return allowed


def verify_reply(
    reply: str,
    facts: Dict[str, Any],
    matched_sops: Iterable[SOPModel],
    allowed_sop_ids: List[str],
    rendered_advice: Iterable[str] = (),
) -> tuple:
    """
    Check that the reply is grounded in the engine's output.

    Two invariants:
      1. Every number in the reply appears in the allowed set.
      2. Every SOP id cited appears among the matched SOP ids.

    Returns `(is_valid, errors)`.
    """
    errors: List[str] = []

    matched_list = list(matched_sops or [])
    allowed_numbers = collect_allowed_numbers(facts, matched_list, rendered_advice)
    reply_numbers = extract_numbers_from_text(reply)

    for number in reply_numbers:
        rounded = round(number, 1)
        if rounded in allowed_numbers or float(int(number)) in allowed_numbers:
            continue
        errors.append(f"Unallowed numeric value in reply: {number}")

    permitted_ids = {str(pid).upper() for pid in (allowed_sop_ids or [])}
    for cited in SOP_ID_REGEX.findall(reply or ""):
        if cited.upper() not in permitted_ids:
            errors.append(f"Cited SOP ID '{cited}' is not among matched SOP IDs: {allowed_sop_ids}")

    return len(errors) == 0, errors


def requires_policy_citation(reply: str, allowed_sop_ids: List[str]) -> bool:
    """True when policy is in play but the reply cites nothing."""
    if not allowed_sop_ids:
        return False
    return not SOP_ID_REGEX.search(reply or "")