"""
API tests.

The /chat endpoint is exercised against a scripted model so the response
contract is asserted without a network call.
"""
import json

from fastapi.testclient import TestClient

from app.config import BASE_DIR
from app.main import app

from conftest import Ref, ScriptedTurn, fake_forecast, fake_geocode

client = TestClient(app)


def _fixture(name: str) -> dict:
    path = BASE_DIR / "backend" / "evals" / "fixtures" / name
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


BOPAL = {
    "bhopal": [
        {
            "name": "Bhopal",
            "admin1": "Madhya Pradesh",
            "country": "India",
            "latitude": 23.2599,
            "longitude": 77.4126,
        }
    ]
}


def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["sops_loaded"] >= 12
    assert "cycling" in data["taxonomy"]


def test_sops_endpoint():
    response = client.get("/sops")
    assert response.status_code == 200
    data = response.json()
    assert len(data) >= 12
    ids = [item["id"] for item in data]
    assert "SIT-RAIN-SYSTEM-01" in ids


def test_chat_rejects_empty_input():
    assert client.post("/chat", json={"session_id": "s", "message": "  "}).status_code == 400
    assert client.post("/chat", json={"session_id": "  ", "message": "hi"}).status_code == 400


def test_chat_endpoint_full_flow(scripted_model, monkeypatch):
    """The response payload carries the location, policy ids and tool trace."""
    fake_geocode(monkeypatch, BOPAL)
    fake_forecast(monkeypatch, _fixture("bhopal_mild.json"))

    scripted_model(
        [
            ScriptedTurn(calls=[("search_location", {"query": "Bhopal"})]),
            ScriptedTurn(
                calls=[
                    (
                        "get_forecast",
                        {
                            "location_ref": Ref("search_location", "candidates", 0, "ref"),
                            "time_window": "today",
                        },
                    )
                ]
            ),
            ScriptedTurn(
                calls=[
                    (
                        "evaluate_policies",
                        {
                            "forecast_ref": Ref("get_forecast", "forecast_ref"),
                            "activity": "cycling",
                        },
                    )
                ]
            ),
            ScriptedTurn(
                calls=[
                    (
                        "end_turn",
                        {
                            "outcome": "answered",
                            "message": "Good conditions to cycle in Bhopal. "
                            "See EXE-MILD-CONDITIONS-01.",
                        },
                    )
                ]
            ),
        ]
    )

    response = client.post(
        "/chat", json={"session_id": "api_cycle", "message": "cycle in Bhopal?"}
    )

    assert response.status_code == 200
    data = response.json()

    assert data["outcome"] == "answered"
    assert "EXE-MILD-CONDITIONS-01" in data["sop_ids"]
    assert data["location"]["label"].startswith("Bhopal")
    assert data["location"]["source"] == "geocoded"
    assert data["facts_used"]["wind_kmh"] == 12.0
    assert any(t.startswith("tool:") for t in data["trace"])
    assert data["trace"][0] == "guard_input"
    assert data["trace"][-1] == "finalize"


def test_chat_endpoint_smalltalk(scripted_model):
    scripted_model(
        [
            ScriptedTurn(
                calls=[
                    (
                        "end_turn",
                        {
                            "outcome": "out_of_scope",
                            "message": "Hi! What activity and city should I check?",
                        },
                    )
                ]
            )
        ]
    )

    response = client.post("/chat", json={"session_id": "api_hello", "message": "hello"})
    assert response.status_code == 200
    data = response.json()

    assert data["outcome"] == "out_of_scope"
    assert "Hi" in data["reply"]
    assert data["sop_ids"] == []
    assert "guard_input" in data["trace"]


def test_chat_endpoint_no_policy_match(scripted_model, monkeypatch):
    fake_geocode(monkeypatch, BOPAL)
    fake_forecast(monkeypatch, _fixture("bhopal_mild.json"))

    scripted_model(
        [
            ScriptedTurn(calls=[("search_location", {"query": "Bhopal"})]),
            ScriptedTurn(
                calls=[
                    (
                        "get_forecast",
                        {
                            "location_ref": Ref("search_location", "candidates", 0, "ref"),
                            "time_window": "today",
                        },
                    )
                ]
            ),
            ScriptedTurn(
                calls=[
                    (
                        "evaluate_policies",
                        {
                            "forecast_ref": Ref("get_forecast", "forecast_ref"),
                            "activity": "unknown",
                        },
                    )
                ]
            ),
            ScriptedTurn(
                calls=[
                    (
                        "end_turn",
                        {
                            "outcome": "no_sop",
                            "message": "I don't have published guidance for that.",
                        },
                    )
                ]
            ),
        ]
    )

    response = client.post(
        "/chat", json={"session_id": "api_nosop", "message": "should I go hiking?"}
    )
    data = response.json()

    assert data["outcome"] in ("no_sop", "answered")
    if data["outcome"] == "no_sop":
        assert data["sop_ids"] == []
        assert "guidance" in data["reply"].lower()