from typing import List, Tuple, Dict, Any

from app import policy_config
from app.schemas import SOPModel

# How many additional SOPs accompany the primary one in the combined advice.
SECONDARY_LIMIT = 2


def resolve_conflicts(
    matched_items: List[Tuple[SOPModel, str]]
) -> Dict[str, Any]:
    """
    Ranks matched SOPs deterministically:
    1. Precedence ('override' first)
    2. Severity rank (see app/policy_config.yaml -> severity_rank)
    3. SOP ID ascending, as a stable tie-break

    Returns dict with:
    - primary: (SOPModel, advice_text)
    - secondaries: up to SECONDARY_LIMIT further (SOPModel, advice_text)
    - all_sop_ids: every matched SOP ID, in rank order
    - combined_advice: rendered block combining the above
    """
    if not matched_items:
        return {
            "primary": None,
            "secondaries": [],
            "all_sop_ids": [],
            "combined_advice": "",
        }

    # Precedence first, then severity, then SOP ID so the ordering is total and
    # reproducible across runs. An override wins even against a more severe
    # match, which is the point of marking something an override.
    sorted_items = sorted(
        matched_items,
        key=lambda item: (
            item[0].precedence != "override",
            -policy_config.severity_rank(item[0].severity),
            item[0].id,
        ),
    )

    primary = sorted_items[0]
    secondaries = sorted_items[1 : 1 + SECONDARY_LIMIT]
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
