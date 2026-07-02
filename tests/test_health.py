"""Placeholder test proving the app boots and CI is wired (RC1-132 acceptance)."""

from fastapi.testclient import TestClient

from main import app

client = TestClient(app)


def test_healthz_ok():
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_drift_run_is_stubbed():
    resp = client.post("/drift/run")
    assert resp.status_code == 202
    assert resp.json()["status"] == "not_implemented"
