"""FastAPI entrypoint for the TPM automation platform.

Run locally:
    uvicorn main:app --reload

Endpoints:
    GET  /healthz     liveness probe (no auth, no side effects)
    POST /drift/run   run one drift-detection cycle (collect -> ... -> notify)
"""

from __future__ import annotations

import logging
import sys

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from config import settings
from drift.pipeline import run_drift

# Emit the per-run structured JSON summary to stdout (captured by Fly logs),
# independent of uvicorn's own logging config.
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter("%(message)s"))
_run_logger = logging.getLogger("drift.run")
_run_logger.setLevel(logging.INFO)
_run_logger.addHandler(_handler)
_run_logger.propagate = False

app = FastAPI(
    title="TPM Automation Platform",
    version="0.1.0",
    summary="Dependency Drift Detector and shared TPM collectors/store/narrative.",
)


class HealthResponse(BaseModel):
    status: str = "ok"


@app.get("/healthz", response_model=HealthResponse, tags=["ops"])
def healthz() -> HealthResponse:
    """Liveness probe used by CI, Fly.io, and the scheduler."""
    return HealthResponse()


@app.post("/drift/run", tags=["drift"])
def run_drift_endpoint(x_drift_token: str | None = Header(default=None)) -> dict:
    """Run one drift-detection cycle and return the run summary.

    Guarded by X-Drift-Token when DRIFT_RUN_TOKEN is set (the scheduler sends it);
    open when unset for local dev.
    """
    if settings.drift_run_token and x_drift_token != settings.drift_run_token:
        raise HTTPException(status_code=401, detail="invalid or missing X-Drift-Token")
    return run_drift()
