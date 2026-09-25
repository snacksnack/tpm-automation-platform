# OTLP vs ddtrace, diffed on one operation (RC1-450)

The estate's telemetry standard was ddtrace by inertia: every traced service
uses it (RC1-445), but nothing had ever tested the alternative. This lab ran
the drift digest — the exact production code path,
`narrative.drift_digest.build_digest` → one anthropic `Messages.create` —
under both tracers and diffed what landed in Datadog. The result is the
evidence for the standard, now decided: **the estate instruments with ddtrace**
(see "The decision" below).

## Method

`scripts/otel_lab.py` runs the operation on fixture-shaped findings (the
`test_narrative` shapes — no Jira, no Slack, no SQLite) under lab-only service
names nothing queries: `tpm-drift-detector-ddtrace-lab` and
`tpm-drift-detector-otel-lab`, both `env:lab`. Each mode makes one billed
happy-path call (1,443 input / ~320 output tokens, ≈ $0.05 for the pair) and
one error call that 404s before tokens are spent (`claude-nonexistent-rc1-450`).

- **ddtrace side:** the prod wiring exactly — `enable_llm_obs()`, agentless,
  no agent, no collector. Run 2026-09-25 23:16Z; prod's daily spans were also
  used as reference.
- **OTel side:** OpenTelemetry SDK 1.45.0 → OTLP/HTTP → Collector
  (otelcol-contrib 0.161.0, local) → Datadog exporter → intake. The
  **Collector path, not the Agent's OTLP endpoint**, because an Agent becomes
  this account's first billed infra host (the RC1-449 cost; 2026-09-16 price
  check). One manual root span, one manual CLIENT span per call carrying the
  GenAI semantic conventions (`gen_ai.*`), plus
  `opentelemetry-instrumentation-httpx` for whatever OTel could see natively.

Config in `scripts/otel_lab_collector.yaml`. Five spans arrived (indexing lag
~2 minutes); the fetch recipe is the spans search API,
`service:(tpm-drift-detector-otel-lab OR tpm-drift-detector-ddtrace-lab)`.

## The diff

### Naming: Datadog derives OTel identities, and they are worse

| | ddtrace | OTel via Collector |
| --- | --- | --- |
| `operation_name` | `anthropic.request` | `Internal` / `client.request` (derived from span kind) |
| `resource_name` | `Messages.create` | the OTel span name: `drift_digest`, `chat claude-opus-4-8` |
| `type` | `llm` | `custom` / `http` |

The OTel span *name* — the primary identity in the OTel model — is demoted to
`resource_name`, and `operation_name` is machine-derived from `span.kind`
(an INTERNAL span is literally named `Internal`). Nothing on the OTel path is
`type:llm`, even with full `gen_ai.*` attributes: Datadog typed the model call
`http`. And the GenAI semconv span-name convention (`chat {model}`) embeds the
model in `resource_name`, so every model change mints a new resource —
a cardinality trap ddtrace avoids by keeping `Messages.create` stable.

### Tag mapping: unified service tags survive, identity attributes multiply

`service.name` → `service`, `service.version` → `version`, and
`deployment.environment(.name)` → `env` all mapped correctly (both spellings
were sent; `deployment.environment` also survives as a literal tag). The OTel
spans additionally carry `otel.scope.name`, `telemetry.sdk.*`,
`service.instance.id` and `span.kind` as tags, and `host:otlp-lab` from the
exporter config — where agentless ddtrace sends an empty host. Manually set
`gen_ai.*` attributes survive verbatim; ddtrace extracts the same facts
(model, tokens) automatically and *also* writes its own `anthropic.*` and
`gen_ai.*` tags, so the ddtrace span is a superset.

### Error representation: one path reaches Error Tracking, one does not

The ddtrace error span has top-level, faceted `error.type`
(`anthropic.NotFoundError`), `error.message`, `error.stack`, a fingerprint —
and an Error Tracking `issue.id`: the failure auto-joined Error Tracking with
first-seen tracking. The OTel error span has `status: error` and the OTel
status description, but its exception lives inside a JSON `events` blob
(`exception.type/message/stacktrace` as span events), the top-level `error`
field is **null**, and no Error Tracking issue was created. Any query,
monitor or notebook filtering `@error.type:*` is blind to OTel errors as
shipped. (Lab note: `record_exception` inside a `with` block that re-raises
records the exception twice — once manually, once by the context manager.)

### What a span *is*: the vendored-httpx finding

The intended "what OTel sees natively" comparison never produced a span: the
anthropic SDK vendors its own copy of httpx as `httpx2`, so
`opentelemetry-instrumentation-httpx` — which patches the `httpx` package by
import identity — instruments nothing. Verified offline: a plain
`httpx.Client` traces; the anthropic client (transport class
`httpx2.HTTPTransport`) is invisible. ddtrace is immune because its
integration patches the anthropic SDK itself. This is the models disagreeing
about what a span is: ddtrace ships *semantic* integrations (an LLM call),
OTel auto-instrumentation ships *transport* integrations (an HTTP POST) — and
a vendored transport defeats the latter entirely. On the OTel path, LLM
telemetry exists only if you hand-write it.

### Hierarchy and identity systems

ddtrace produced two single-span traces (one per call), each also carrying a
*second* identity — `llmobs_trace_id`/`llmobs_parent_id` and
`p_llm_obs.ml_app: drift-digest-lab` — the LLM Observability overlay. OTel
produced one three-span trace (root + two CLIENT children) with W3C trace ids
and no LLM Obs linkage of any kind: the `gen_ai.*` span did **not** appear in
LLM Observability. There is no OTel path into LLM Obs.

### Ingestion and downstream products

- OTel spans arrive `ingestion_reason:probabilistic`; ddtrace's arrive
  `ingestion_reason:auto` — different sampling regimes with different knobs.
- The exporter disables APM trace metrics by default (needs the Datadog
  Connector), so `trace.*` metrics — and any monitor on them — do not exist
  on this path.
- `host_metadata` was disabled to avoid the infra-host cost, and the billable
  summary confirms no host dimension moved. A host *row* named `otlp-lab`
  still appears in the host list from the span host tag; it is cosmetic and
  ages out. Check billing in `/api/v1/usage/billable-summary`, never the host
  list.

## What wholesale migration would break

Concretely, in this account, today:

- **The LLM Observability layer, entirely** — the binding constraint. Ten
  ml_apps, SLO `dae4404` and monitor 322827096 (ml_app-scoped), the verdict
  monitor 322028416 (`@ml_app` grouping), all three eval judge configs
  (RC1-408/443/444), and every `ml_obs.span.llm.*` cost widget. None of this
  has an OTel equivalent to migrate *to*.
- **Error Tracking for traces** and every `@error.type` query.
- **Trace metrics** and anything monitoring them, absent a Datadog Connector.
- Every query matching `operation_name:anthropic.request` or `type:llm`.
- Plus new permanent infrastructure: a Collector, always on, in an estate
  where everything scales to zero.

What survives: `service`/`env`/`version` continuity, DORA (event-driven, not
span-driven), and the Software Catalog (file-first entities).

## The decision

**The estate's telemetry standard is ddtrace, decided with evidence rather
than by default.** All services emit traces through it (`enable_llm_obs()` or
the Lambda extension — one tracer, RC1-445); the named exceptions stand
(launch-planner is custom-metrics-only by design per RC1-455's ADR-0041 record;
n8n is excluded from everything). OTel's portability is real but purchasable
only by forfeiting LLM Observability, which is the estate's centerpiece.
Revisit if Datadog ships OTel-native LLM Obs ingestion (GenAI semconv →
ml_app), which would remove the binding constraint.

The lab is torn down: the Collector was local and is stopped, the lab services
receive nothing further, and their spans age out with retention. Nothing
production-facing was touched.
