"""FastAPI entrypoint for the TPM automation platform.

Run locally:
    uvicorn main:app --reload

Endpoints:
    GET  /healthz     liveness probe (no auth, no side effects)
    POST /drift/run   trigger a drift-detection run — stub until [6/9]/[9/9]
"""

from __future__ import annotations

from fastapi import FastAPI, status
from pydantic import BaseModel

app = FastAPI(
    title="TPM Automation Platform",
    version="0.1.0",
    summary="Dependency Drift Detector and shared TPM collectors/store/narrative.",
)


class HealthResponse(BaseModel):
    status: str = "ok"


class DriftRunResponse(BaseModel):
    status: str
    detail: str


@app.get("/healthz", response_model=HealthResponse, tags=["ops"])
def healthz() -> HealthResponse:
    """Liveness probe used by CI, Fly.io, and the scheduler."""
    return HealthResponse()


@app.post(
    "/drift/run",
    response_model=DriftRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["drift"],
)
def run_drift() -> DriftRunResponse:
    """Trigger a drift-detection run.

    Stub for [1/9]. The real pipeline (collect -> graph -> rules -> narrate ->
    notify) is wired up in [6/9] and scheduled in [9/9].
    """
    return DriftRunResponse(
        status="not_implemented",
        detail="Drift pipeline not wired yet — see RC1-137 [6/9] and RC1-140 [9/9].",
    )
