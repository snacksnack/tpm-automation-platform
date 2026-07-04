"""API tests — healthz and the /drift/run token gate (no live pipeline in CI)."""

from fastapi.testclient import TestClient

import main

client = TestClient(main.app)


def test_healthz_ok():
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_drift_run_open_when_no_token(monkeypatch):
    monkeypatch.setattr(main.settings, "drift_run_token", None)
    monkeypatch.setattr(main, "run_drift", lambda **kw: {"event": "drift_run", "findings": 0})
    resp = client.post("/drift/run")
    assert resp.status_code == 200
    assert resp.json()["event"] == "drift_run"


def test_drift_run_requires_token_when_set(monkeypatch):
    monkeypatch.setattr(main.settings, "drift_run_token", "s3cret")
    monkeypatch.setattr(main, "run_drift", lambda **kw: {"ok": True})

    assert client.post("/drift/run").status_code == 401  # missing token
    assert client.post("/drift/run", headers={"X-Drift-Token": "wrong"}).status_code == 401
    ok = client.post("/drift/run", headers={"X-Drift-Token": "s3cret"})
    assert ok.status_code == 200 and ok.json() == {"ok": True}
