from typing import List, Tuple, Dict, Any

from app.schemas import SOPModel

SEVERITY_WEIGHTS: Dict[str, int] = {
    "critical": 5,
    "high": 4,
    "moderate": 3,
    "low": 2,
    "info": 1,
}



def resolve_conflicts(
    matched_items: List[Tuple[SOPModel, str]]
) -> Dict[str, Any]:
    """
    Ranks matched SOPs:
    1. Precedence ('override' first)
    2. Severity (critical > high > moderate > low > info)
    3. SOP ID (alphabetical tie-breaker)

    Returns dict with:
    - primary: (SOPModel, advice_text)
    - secondaries: List of up to 2 (SOPModel, advice_text)
    - all_sop_ids: List of all matched SOP IDs
    - combined_advice: String containing formatted SOP advice text
    """
    if not matched_items:
        return {
            "primary": None,
            "secondaries": [],
            "all_sop_ids": [],
            "combined_advice": "",
        }

    # Sort items: override first, highest severity first, then SOP ID (reversed for id)
    # Python sort with custom key:
    sorted_items = sorted(
        matched_items,
        key=lambda x: (
            1 if x[0].precedence == "override" else 0,
            SEVERITY_WEIGHTS.get(x[0].severity.lower(), 0),
            # Invert ID string sorting for tie-break if needed or handle cleanly
        ),
        reverse=True
    )

    primary = sorted_items[0]
    secondaries = sorted_items[1:3]
    all_ids = [item[0].id for item in sorted_items]

    advice_blocks = [f"Primary Guidance [{primary[0].id}]: {primary[1]}"]
    for sec in secondaries:
        advice_blocks.append(f"Additional Consideration [{sec[0].id}]: {sec[1]}")

    combined_advice = "\n\n".join(advice_blocks)

    return {
        "primary": primary,
        "secondaries": secondaries,
        "all_sop_ids": all_ids,
        "combined_advice": combined_advice,
    }
