# API reference

HTTP API served by `uvicorn orchestrator:app` (FastAPI). Bodies are JSON. The interactive schema is
at `/docs` and the machine-readable one at `/openapi.json` while the server runs.
`tests/test_reference_docs.py` fails the build if a route exists that this page does not list.

## Intent

The one input type. Strict: wrong types are rejected rather than coerced, and unknown fields are
rejected ([ADR-0003](../design/adr/0003-intent-schema-contract.md)).

| Field | Type | Constraint |
|---|---|---|
| `name` | string | Kubernetes name: `^[a-z0-9]([-a-z0-9]*[a-z0-9])?$`, at most 53 characters (Helm's release-name limit) |
| `replicas` | integer | 1 to 10 |
| `environment` | string | `dev`, `staging` or `prod` |

```json
{"name": "sample-nf", "replicas": 2, "environment": "dev"}
```

## Routes

### `POST /deployments` — deploy an intent

`helm upgrade --install` with the intent rendered to chart values. Never takes ownership of a field
another manager holds ([ADR-0008](../design/adr/0008-forcing-field-ownership-is-an-explicit-operation.md)).

`201`:

```json
{"release": "sample-nf", "status": "deployed", "revision": 1,
 "values": {"replicaCount": 2, "environment": "dev"}}
```

`409` when another field manager owns a field this intent would change. Resubmitting will not help;
see `/repair`. Other errors: [error model](#error-model).

### `POST /deployments/{name}/repair` — deploy and take ownership of contested fields

The same deploy with `--force-conflicts`. Explicit and separate, because it overrides whatever else
was writing to those fields. The body is an intent whose `name` must equal `{name}`, otherwise `400`.

`200`: the `POST /deployments` body plus `"forced": true`.

### `GET /deployments/{name}` — lifecycle state

State is derived from Helm and live pods on every read, never stored
([ADR-0005](../design/adr/0005-derive-lifecycle-state-from-cluster-signals.md)).

`200`:

```json
{"release": "sample-nf", "state": "INSTANTIATED", "helm_status": "deployed",
 "desired_replicas": 2, "ready_replicas": 2,
 "last_operation": {"revision": 1, "state": "COMPLETED", "description": "Install complete"},
 "pods": [{"phase": "Running", "reason": null, "ready": true}]}
```

| Field | Values |
|---|---|
| `state` | `NOT_INSTANTIATED`, `INSTANTIATING`, `INSTANTIATED`, `FAILED`. What is true of the network function ([lifecycle states](../design/lifecycle-states.md)) |
| `last_operation.state` | `PROCESSING`, `COMPLETED`, `FAILED`, `UNKNOWN`. What happened to the last Helm operation, which is not the same thing ([ADR-0009](../design/adr/0009-the-operation-is-not-the-network-function.md)) |
| `desired_replicas` | From the last revision actually in effect, not the last one submitted |
| `ready_replicas` | Pods whose containers all report ready |

A release that does not exist is `200` with `state: NOT_INSTANTIATED`. A cluster that cannot be read
is **never** a state: it is an error ([ADR-0006](../design/adr/0006-instantiated-means-the-intent-is-satisfied.md)).

### `DELETE /deployments/{name}` — tear down

Idempotent. `200` with `"uninstalled": true`, or `false` if there was nothing to uninstall:

```json
{"release": "sample-nf", "state": "NOT_INSTANTIATED", "uninstalled": true}
```

### `POST /intents/validate` — validate without deploying

`200` `{"valid": true, "intent": {...}}`, or `422` with the validation errors.

### `POST /intents/translate` — free text to a candidate intent

Body `{"text": "..."}`, at most 500 characters. Returns a **candidate**: it has passed the same
intent validation, but nothing is deployed until it is sent to `POST /deployments`
([ADR-0012](../design/adr/0012-the-translator-is-untrusted-input.md)).

`200`:

```json
{"intent": {"name": "edge-nf", "replicas": 2, "environment": "dev"},
 "translator": "rules", "warnings": []}
```

`422` when the text is empty, too long, ambiguous, or translates to something the schema rejects.
The route uses the rule-based translator.

### `GET /intents/schema`

The intent's JSON Schema, generated from the model.

### `GET /healthz` — liveness

Always `200` `{"status": "ok"}` while the process runs. Says nothing about the cluster, deliberately.

### `GET /readyz` — readiness

`200` `{"status": "ready", "cluster": "reachable"}` when helm and kubectl are installed and the API
server answers its own `/readyz`. Otherwise the [error model](#error-model) says which end failed:
a missing tool is `500`, an overloaded host or a throttled cluster is `503` with `Retry-After`, and
an unreachable cluster is `503` without it.

### `GET /metrics`

Prometheus text exposition. Every release is reconciled at scrape time, inside one budget. Series:
[metrics reference](metrics.md).

## Error model

Errors are `{"detail": "..."}`. The status code says **who** failed and **whether to retry**. The
detail says what was observed, with the numbers.

| Status | `Retry-After` | Meaning | Retry? |
|---|---|---|---|
| `400` | — | The request contradicts itself (repair name differs from the path) | Fix the request |
| `409` | — | Another field manager owns a field this intent would change | No. Use `/repair` if the orchestrator should own it |
| `422` | — | The intent or the text failed validation | Fix the request |
| `500` | — | **This host cannot run helm or kubectl**: missing or not executable. Nothing changes until someone installs it ([runbook](../operations/runbooks/orchestrator-misconfigured.md)) | No |
| `502` | — | The cluster answered and refused the operation | Not as-is |
| `503` | `5` | **The cluster is reachable and not serving this client**: throttled or overloaded ([runbook](../operations/runbooks/cluster-throttling.md)) | Yes, after `Retry-After` |
| `503` | `5` | **This host is overloaded or out of processes or threads**: the detail names the host and, where measured, the cluster's own round trip ([runbook](../operations/runbooks/scrape-over-budget.md#the-orchestrators-host-is-overloaded)) | Yes, after `Retry-After` |
| `503` | — | **The cluster could not be reached** ([runbook](../operations/runbooks/control-plane-unreachable.md)) | Once it is back |

Each row is a failure a drill produced. The classification order, and the evidence behind each
decision, are in [architecture](../design/architecture.md#failure-classification).
