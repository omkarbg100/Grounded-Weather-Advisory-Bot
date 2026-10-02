import json
from pathlib import Path
import pytest
from app.graph.builder import create_weather_sop_graph
from app.config import BASE_DIR


def test_graph_compilation_and_execution():
    app = create_weather_sop_graph()
    assert app is not None

    # Test smalltalk execution
    config = {"configurable": {"thread_id": "test_session_1"}}
    res = app.invoke({"message": "hello", "session_id": "test_session_1"}, config=config)
    assert res["outcome"] == "out_of_scope"
    assert "guard_input" in res["trace"]
    assert "respond_no_scope" in res["trace"]


def test_graph_full_flow_with_fixture():
    app = create_weather_sop_graph()
    config = {"configurable": {"thread_id": "test_session_2"}}

    fixture_path = BASE_DIR / "backend" / "evals" / "fixtures" / "bhopal_mild.json"
    with open(fixture_path, "r", encoding="utf-8") as f:
        mild_fixture = json.load(f)

    input_state = {
        "message": "is it good to cycle in Bhopal 23.26, 77.41?",
        "session_id": "test_session_2",
        "raw_weather": mild_fixture,
    }

    res = app.invoke(input_state, config=config)
    assert res["outcome"] == "answered"
    assert len(res["matched_sops"]) > 0
    assert any(s[0].id == "EXE-MILD-CONDITIONS-01" for s in res["matched_sops"])
