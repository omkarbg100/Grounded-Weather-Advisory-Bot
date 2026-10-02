from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["sops_loaded"] >= 12


def test_sops_endpoint():
    response = client.get("/sops")
    assert response.status_code == 200
    data = response.json()
    assert len(data) >= 12
    ids = [item["id"] for item in data]
    assert "SIT-RAIN-SYSTEM-01" in ids


def test_chat_endpoint_smalltalk():
    response = client.post("/chat", json={"session_id": "test_api_1", "message": "hello"})
    assert response.status_code == 200
    data = response.json()
    assert data["outcome"] == "out_of_scope"
    assert "reply" in data
    assert "trace" in data
