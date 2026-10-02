from pathlib import Path
from typing import List, Set, Union, Dict, Any
import yaml

from app.schemas import SOPModel, SOPRegistry, WhenGroup, SingleCondition, FuzzyScoring, FuzzyCriterion, ComparisonOp
from app.engine.facts import KNOWN_FACT_NAMES
from app.config import SOPS_DIR

VALID_OPS: Set[str] = {">", ">=", "<", "<=", "==", "between", "in"}
VALID_SEVERITIES: Set[str] = {"info", "low", "moderate", "high", "critical"}


def _validate_facts_and_ops(condition: Union[SingleCondition, WhenGroup, Dict[str, Any]]) -> None:
    if isinstance(condition, dict):
        if "fact" in condition:
            fact = condition.get("fact")
            if fact not in KNOWN_FACT_NAMES:
                raise ValueError(f"Unknown fact name '{fact}' in SOP condition")
            op = condition.get("op")
            if op not in VALID_OPS:
                raise ValueError(f"Unknown comparison operator '{op}' in SOP condition")
        if "all" in condition and isinstance(condition["all"], list):
            for item in condition["all"]:
                _validate_facts_and_ops(item)
        if "any" in condition and isinstance(condition["any"], list):
            for item in condition["any"]:
                _validate_facts_and_ops(item)
    elif isinstance(condition, SingleCondition):
        if condition.fact not in KNOWN_FACT_NAMES:
            raise ValueError(f"Unknown fact name '{condition.fact}' in SOP condition")
        if condition.op not in VALID_OPS:
            raise ValueError(f"Unknown comparison operator '{condition.op}' in SOP condition")
    elif isinstance(condition, WhenGroup):
        if condition.all:
            for item in condition.all:
                _validate_facts_and_ops(item)
        if condition.any:
            for item in condition.any:
                _validate_facts_and_ops(item)


def load_sops(sops_dir: Path = SOPS_DIR) -> SOPRegistry:
    """
    Reads all YAML files in sops_dir, validates SOP schemas, fact names, operators,
    duplicate IDs, and severity values at startup.
    Returns SOPRegistry with loaded SOPs and activity taxonomy.
    """
    if not sops_dir.exists():
        raise FileNotFoundError(f"SOPs directory not found: {sops_dir}")

    sops: List[SOPModel] = []
    seen_ids: Set[str] = set()
    taxonomy_set: Set[str] = set()

    yaml_files = sorted(list(sops_dir.glob("*.yaml")) + list(sops_dir.glob("*.yml")))
    if not yaml_files:
        raise ValueError(f"No YAML SOP files found in directory {sops_dir}")

    for file_path in yaml_files:
        with open(file_path, "r", encoding="utf-8") as f:
            content = yaml.safe_load(f)

        if not content or not isinstance(content, list):
            continue

        for item in content:
            # Validate Pydantic SOPModel
            try:
                sop = SOPModel.model_validate(item)
            except Exception as e:
                raise ValueError(f"Invalid SOP in {file_path.name}: {str(e)}") from e

            # Check duplicate IDs
            if sop.id in seen_ids:
                raise ValueError(f"Duplicate SOP ID found: '{sop.id}' in {file_path.name}")
            seen_ids.add(sop.id)

            # Check fact names and operators in 'when'
            if sop.when:
                _validate_facts_and_ops(sop.when)

            # Check fact names and operators in 'scoring'
            if sop.scoring:
                for criterion in sop.scoring.criteria:
                    if criterion.fact not in KNOWN_FACT_NAMES:
                        raise ValueError(f"Unknown fact name '{criterion.fact}' in fuzzy scoring SOP '{sop.id}'")
                    if criterion.op not in VALID_OPS:
                        raise ValueError(f"Unknown operator '{criterion.op}' in fuzzy scoring SOP '{sop.id}'")

            sops.append(sop)

            # Accumulate taxonomy
            for tag in sop.applies_to:
                if tag != "*":
                    taxonomy_set.add(tag)

    return SOPRegistry(sops=sops, taxonomy=sorted(list(taxonomy_set)))
