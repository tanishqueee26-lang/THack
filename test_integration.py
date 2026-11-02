import pytest
from fastapi.testclient import TestClient

from backend import config
from backend.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def no_credentials(monkeypatch):
    for k in ("CLIENT_ID", "CLIENT_SECRET", "ENCRYPTION_KEY"):
        monkeypatch.setattr(config, k, "")


def test_status_is_simulation_and_disconnected():
    s = client.get("/api/integration/status").json()
    assert s["mode"] == "simulation" and s["connected"] is False and s["account"] is None
    assert "TWITCH_CLIENT_ID" in s["simulation_reasons"]


def test_simulated_events_are_flagged():
    ev = client.post("/api/simulate/event", json={"type": "channel.follow"}).json()
    assert ev["simulated"] is True and ev["summary"].startswith("[SIMULATED]")


def test_login_and_clips_unavailable_without_credentials():
    assert client.get("/auth/login", follow_redirects=False).status_code == 503
    assert client.post("/api/clips").status_code == 401


def test_simulation_blocked_when_live(monkeypatch):
    for k in ("CLIENT_ID", "CLIENT_SECRET", "ENCRYPTION_KEY"):
        monkeypatch.setattr(config, k, "real-value")
    assert client.post("/api/simulate/event", json={"type": "channel.follow"}).status_code == 403
