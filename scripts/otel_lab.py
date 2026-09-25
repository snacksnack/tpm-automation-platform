"""RC1-450 OTLP lab: the drift digest traced by ddtrace and by OpenTelemetry.

Runs the production code path (narrative.drift_digest.build_digest -> one real
anthropic Messages.create) under lab-only service names so the two tracers'
output can be diffed in Datadog APM. Nothing production-facing: fixture-shaped
findings (the test_narrative shapes), no Jira, no Slack, no SQLite, and no
dashboard, monitor, scorecard rule or catalog entity references the lab
services. Each mode makes one billed happy-path call and one unbilled error
call (a nonexistent model 404s before tokens are spent).

Both modes are run with the OTel packages importable, so imports stay at the
top per convention:

    DD_ENV=lab uv run \
      --with opentelemetry-sdk \
      --with opentelemetry-exporter-otlp-proto-http \
      --with opentelemetry-instrumentation-httpx \
      python scripts/otel_lab.py {ddtrace|otel}

`otel` needs the Collector listening on 127.0.0.1:4318 (config in
scripts/otel_lab_collector.yaml) and no DD_* at all — the Collector holds the
key. `ddtrace` ships agentless, exactly like the deployed service, and needs
DD_API_KEY in the process environment at interpreter start (ddtrace snapshots
its config at import, so the script cannot export it later — Fly does the
same thing with a real env var). ANTHROPIC_API_KEY comes from `.env` via
config.settings as in prod.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from types import SimpleNamespace

import anthropic
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind, Status, StatusCode, format_trace_id

from collectors.models import Issue, ProjectSnapshot
from narrative import drift_digest
from observability import enable_llm_obs
from store.models import Finding

OTEL_SERVICE = "tpm-drift-detector-otel-lab"
DDTRACE_SERVICE = "tpm-drift-detector-ddtrace-lab"
BAD_MODEL = "claude-nonexistent-rc1-450"


def _git_sha() -> str:
    out = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=False
    )
    return out.stdout.strip() or "unknown"


# --- the same operation prod runs daily, on fixture-shaped data --------------


def _issue(key: str, summary: str, **kw) -> Issue:
    return Issue(
        key=key, summary=summary, status=kw.get("status", "In Progress"),
        status_category="In Progress", priority="High",
        assignee_name=kw.get("owner"), due=kw.get("due"), start=kw.get("start"),
    )


def _snapshot() -> ProjectSnapshot:
    return ProjectSnapshot(
        project_key="RC1",
        issues=[
            _issue("RC1-1", "Vendor security review", owner="Reid", due=date(2026, 7, 23)),
            _issue("RC1-2", "Launch readiness", owner="Dana", start=date(2026, 7, 24)),
            _issue("RC1-9", "Analytics dashboard", owner="Kim", start=date(2026, 7, 30)),
        ],
    )


def _findings() -> list[Finding]:
    def finding(rule, down, up, sev, bucket):
        return Finding(
            rule_type=rule, downstream=down, upstream=up, severity=sev,
            severity_bucket=bucket, detail=f"{up} -> {down} drift (RC1-450 lab)",
            run_id=5, first_seen_run=5,
        )

    return [
        finding("unabsorbed_slip", "RC1-2", "RC1-1", 30.0, "red"),
        finding("stale_dependency", "RC1-9", "RC1-1", 10.0, "yellow"),
    ]


def _run_operation(client, label: str) -> None:
    """One happy-path digest and one guaranteed-error call, like-for-like."""
    digest = drift_digest.build_digest(_findings(), _snapshot(), client=client)
    usage = drift_digest.last_usage
    print(json.dumps({
        "lab": label, "call": "happy", "subject": digest.subject,
        "input_tokens": usage.input_tokens if usage else None,
        "output_tokens": usage.output_tokens if usage else None,
    }))
    try:
        drift_digest.build_digest(_findings(), _snapshot(), client=client, model=BAD_MODEL)
    except anthropic.NotFoundError as exc:
        print(json.dumps({"lab": label, "call": "error", "caught": type(exc).__name__}))


# --- OTel side: manual GenAI-semconv span + httpx auto-instrumentation -------


class _OTelTracedClient:
    """The narrowest OTel equivalent of ddtrace's anthropic integration: a
    CLIENT span named per the GenAI semantic conventions around the one call
    build_digest makes. The httpx auto-instrumentation adds what OTel sees
    natively underneath — an HTTP POST, not an LLM call."""

    def __init__(self, inner, tracer):
        self._inner = inner
        self._tracer = tracer
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        model = kwargs.get("model", "unknown")
        with self._tracer.start_as_current_span(f"chat {model}", kind=SpanKind.CLIENT) as span:
            span.set_attribute("gen_ai.operation.name", "chat")
            span.set_attribute("gen_ai.provider.name", "anthropic")
            span.set_attribute("gen_ai.request.model", model)
            try:
                resp = self._inner.messages.create(**kwargs)
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                raise
            usage = getattr(resp, "usage", None)
            if usage is not None:
                span.set_attribute("gen_ai.usage.input_tokens", usage.input_tokens)
                span.set_attribute("gen_ai.usage.output_tokens", usage.output_tokens)
            span.set_attribute("gen_ai.response.model", getattr(resp, "model", model))
            return resp


def _run_otel() -> None:
    sha = _git_sha()
    resource = Resource.create({
        "service.name": OTEL_SERVICE,
        "service.version": sha,
        # Both spellings: the current semconv name and the one older Datadog
        # exporter mappings read. Which survives as `env` is a lab finding.
        "deployment.environment.name": "lab",
        "deployment.environment": "lab",
    })
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint="http://127.0.0.1:4318/v1/traces"))
    )
    trace.set_tracer_provider(provider)
    HTTPXClientInstrumentor().instrument()
    tracer = trace.get_tracer("rc1450.otel_lab")

    client = _OTelTracedClient(drift_digest._default_client(), tracer)
    with tracer.start_as_current_span("drift_digest") as root:
        print(json.dumps({"lab": "otel", "trace_id": format_trace_id(
            root.get_span_context().trace_id)}))
        try:
            _run_operation(client, "otel")
        except Exception as exc:  # the error call's exception ends the root span too
            root.record_exception(exc)
            root.set_status(Status(StatusCode.ERROR, str(exc)))
    provider.force_flush()
    provider.shutdown()


# --- ddtrace side: exactly the prod wiring, lab names -------------------------


def _run_ddtrace() -> None:
    if not enable_llm_obs("drift-digest-lab", service=DDTRACE_SERVICE):
        sys.exit("ddtrace mode: LLMObs did not enable (missing DD_API_KEY?)")
    try:
        _run_operation(drift_digest._default_client(), "ddtrace")
    finally:
        from ddtrace.llmobs import LLMObs  # documented optional-dep exception

        LLMObs.flush()


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode not in ("ddtrace", "otel"):
        sys.exit("usage: otel_lab.py {ddtrace|otel}")
    if mode == "otel":
        _run_otel()
    else:
        _run_ddtrace()


if __name__ == "__main__":
    main()
