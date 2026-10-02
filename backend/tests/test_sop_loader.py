import pytest
import tempfile
from pathlib import Path
import yaml

from app.tools.sop_loader import load_sops
from app.config import SOPS_DIR


def test_load_sops_valid():
    registry = load_sops(SOPS_DIR)
    assert len(registry.sops) >= 12
    assert "cycling" in registry.taxonomy
    assert "running" in registry.taxonomy
    sop_ids = [s.id for s in registry.sops]
    assert "SIT-RAIN-SYSTEM-01" in sop_ids
    assert "LEI-PICNIC-01" in sop_ids


def test_load_sops_duplicate_id():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        yaml_content = [
            {
                "id": "DUP-01",
                "category": "test",
                "title": "Test SOP 1",
                "severity": "low",
                "applies_to": ["test"],
                "when": {"all": [{"fact": "temp_c", "op": ">", "value": 20}]},
                "advice": "Test advice 1"
            },
            {
                "id": "DUP-01",
                "category": "test",
                "title": "Test SOP 2",
                "severity": "low",
                "applies_to": ["test"],
                "when": {"all": [{"fact": "temp_c", "op": ">", "value": 20}]},
                "advice": "Test advice 2"
            }
        ]
        with open(tmp_path / "test.yaml", "w") as f:
            yaml.dump(yaml_content, f)

        with pytest.raises(ValueError, match="Duplicate SOP ID"):
            load_sops(tmp_path)


def test_load_sops_unknown_fact():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        yaml_content = [
            {
                "id": "UNF-01",
                "category": "test",
                "title": "Test SOP",
                "severity": "low",
                "applies_to": ["test"],
                "when": {"all": [{"fact": "non_existent_fact", "op": ">", "value": 20}]},
                "advice": "Test advice"
            }
        ]
        with open(tmp_path / "test.yaml", "w") as f:
            yaml.dump(yaml_content, f)

        with pytest.raises(ValueError, match="Unknown fact name"):
            load_sops(tmp_path)


def test_load_sops_bad_severity():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        yaml_content = [
            {
                "id": "BAD-SEV-01",
                "category": "test",
                "title": "Test SOP",
                "severity": "ultra_extreme",
                "applies_to": ["test"],
                "when": {"all": [{"fact": "temp_c", "op": ">", "value": 20}]},
                "advice": "Test advice"
            }
        ]
        with open(tmp_path / "test.yaml", "w") as f:
            yaml.dump(yaml_content, f)

        with pytest.raises(ValueError):
            load_sops(tmp_path)
