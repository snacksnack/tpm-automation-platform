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
import sys

try:  # documented optional-dep exception: ddtrace is absent in minimal envs
    from ddtrace.llmobs import LLMObs
except ImportError:  # pragma: no cover - exercised only without ddtrace
    LLMObs = None


def enable_llm_obs(ml_app: str, *, service: str | None = None) -> bool:
    """Turn on tracing for this process, or quietly decline. Returns whether
    tracing is on; safe to call more than once.

    RC1-331 (mirrored from agent-evals v0.4.1): `LLMObs.enable()` patches
    ddtrace's entire LLM integration list with `raise_errors=True`, so a
    module-name collision or version mismatch would crash the billed stage
    or the drift service's boot for the sake of its decoration. Only the
    anthropic integration is left on, and any failure to start tracing is
    a decline, not an error.
    """
    if LLMObs is None or not os.environ.get("DD_API_KEY"):
        return False
    _restrict_patching_to_anthropic()
    try:
        LLMObs.enable(
            ml_app=ml_app,
            agentless_enabled=True,
            site=os.environ.get("DD_SITE", "datadoghq.com"),
            service=service or ml_app,
        )
    except Exception as exc:
        print(f"llmobs: tracing disabled, enable() failed: {exc}", file=sys.stderr)
        return False
    return True


def _llm_integration_modules() -> tuple[str, ...]:
    """The module names `LLMObs.enable()` would patch; empty when unknown.

    Read from ddtrace's own constants — the same two lists its
    `_patch_integrations` concatenates — so the set tracks the installed
    version. Private imports, guarded: if they move in a future ddtrace we
    fall back to patching everything, and the try/except above still keeps
    the process alive.
    """
    try:
        from ddtrace.llmobs._constants import SUPPORTED_LLMOBS_INTEGRATIONS
        from ddtrace.llmobs._llmobs import _INTEGRATIONS_W_PROPAGATION_SUPPORT
    except ImportError:  # pragma: no cover - exercised only on a moved layout
        return ()
    modules = set(SUPPORTED_LLMOBS_INTEGRATIONS.values())
    modules |= set(_INTEGRATIONS_W_PROPAGATION_SUPPORT.values())
    return tuple(modules)


def _restrict_patching_to_anthropic() -> None:
    """Env-default every non-anthropic LLM integration off (RC1-331).

    setdefault, not setenv: an explicitly configured `DD_TRACE_<X>_ENABLED`
    in the environment still wins.
    """
    for module in _llm_integration_modules():
        if module == "anthropic":
            continue
        os.environ.setdefault(f"DD_TRACE_{module.upper().replace('-', '_')}_ENABLED", "false")
