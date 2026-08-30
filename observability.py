"""Datadog LLM Observability for the platform's billed stages (RC1-322).

One helper, called at every entry point that drives a model — the kpi
define/instrument/narrate CLIs and the drift service. With `DD_API_KEY` in
the environment, each Anthropic call becomes an LLM span (model, tokens,
latency, estimated cost) under the given `ml_app` in Datadog's LLM
Observability view; without it the call is a no-op, so dev machines and CI
run identical code. Agentless on purpose: these are laptop/launchd/Fly
processes with no local Datadog agent daemon.

This is deliberately not `agent_evals.llmobs`: agent-evals lives in the
`[dev]` extra and never ships in the Docker image (RC1-265), while the drift
service bills in production. The evals CLI — dev-only by design — uses the
agent-evals module and its per-case spans; everything deployed or scheduled
uses this one. Both configure the same tracer.

ml_app is the agent, not the repo: `kpi-agent` for the KPI stages,
`drift-digest` for the drift service — the LLM Observability app list then
reads as the fleet inventory.
"""

from __future__ import annotations

import os

try:  # documented optional-dep exception: ddtrace is absent in minimal envs
    from ddtrace.llmobs import LLMObs
except ImportError:  # pragma: no cover - exercised only without ddtrace
    LLMObs = None


def enable_llm_obs(ml_app: str, *, service: str | None = None) -> bool:
    """Turn on tracing for this process, or quietly decline. Returns whether
    tracing is on; safe to call more than once."""
    if LLMObs is None or not os.environ.get("DD_API_KEY"):
        return False
    LLMObs.enable(
        ml_app=ml_app,
        agentless_enabled=True,
        site=os.environ.get("DD_SITE", "datadoghq.com"),
        service=service or ml_app,
    )
    return True
