# M1 — Intent validation endpoint

**Goal:** make the intent contract executable, so a malformed intent is rejected at the API
boundary with a field-level reason instead of failing later inside a deploy.

Contract rationale is in [ADR-0003](../design/adr/0003-intent-schema-contract.md). This page is the
record of building and verifying it on 2026-07-22.

## What exists now

Two routes on the FastAPI app, plus the `Intent` model in `orchestrator/intent.py` that both use.

- `POST /intents/validate` validates a JSON body against the model.
- `GET /intents/schema` returns the JSON Schema generated from that same model.

Started with:

```
uvicorn orchestrator:app --port 8000
```

### A valid intent

```
curl -s -X POST localhost:8000/intents/validate \
  -H 'content-type: application/json' \
  -d @examples/intent-valid.json
```

Request body (`examples/intent-valid.json`):

```json
{
  "name": "sample-nf",
  "replicas": 2,
  "environment": "dev"
}
```

Response, `HTTP 200`:

```json
{"valid":true,"intent":{"name":"sample-nf","replicas":2,"environment":"dev"}}
```

The echoed `intent` is the model's own output, so what comes back is what a deploy would receive.

### The published schema

```
curl -s localhost:8000/intents/schema
```

`HTTP 200`:

```json
{
  "additionalProperties": false,
  "properties": {
    "name": {"maxLength": 63, "pattern": "^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", "title": "Name", "type": "string"},
    "replicas": {"maximum": 10, "minimum": 1, "title": "Replicas", "type": "integer"},
    "environment": {"enum": ["dev", "staging", "prod"], "title": "Environment", "type": "string"}
  },
  "required": ["name", "replicas", "environment"],
  "title": "Intent",
  "type": "object"
}
```

`maxLength` on `name` is 53 from 2026-07-22 onward, not the 63 captured above. The first real
deploy showed that a name Helm rejects was passing validation here; see
[the M2 build log](m2-deploy-engine.md).

## The three rejections

All three return `HTTP 422` with a `detail` array. Bodies below are exactly what the endpoint
returned, reformatted for width only.

### Required field missing

`examples/intent-missing-field.json` omits `replicas`.

```json
{
  "detail": [
    {
      "type": "missing",
      "loc": ["body", "replicas"],
      "msg": "Field required",
      "input": {"name": "sample-nf", "environment": "dev"}
    }
  ]
}
```

### Wrong type

`examples/intent-wrong-type.json` sends `"replicas": "two"`.

```json
{
  "detail": [
    {
      "type": "int_type",
      "loc": ["body", "replicas"],
      "msg": "Input should be a valid integer",
      "input": "two"
    }
  ]
}
```

### Enum violation

`examples/intent-bad-enum.json` sends `"environment": "production"`.

```json
{
  "detail": [
    {
      "type": "literal_error",
      "loc": ["body", "environment"],
      "msg": "Input should be 'dev', 'staging' or 'prod'",
      "input": "production",
      "ctx": {"expected": "'dev', 'staging' or 'prod'"}
    }
  ]
}
```

The `name` pattern behaves the same way. Posting `"name": "Sample_NF"` returns `422` with type
`string_pattern_mismatch` and a `ctx.pattern` echoing the regex, which is the check that keeps an
intent from producing an illegal Helm release name at M2.

## What broke

The endpoint and its own published schema disagreed.

Having two things that both claim to describe the contract is worth a check, so I fetched
`/intents/schema` and ran a handful of payloads through it with the independent `jsonschema`
library, comparing each verdict against the endpoint's status code:

```
valid:         schema_errors=[]                                  endpoint_status=200
string_number: schema_errors=["'3' is not of type 'integer'"]    endpoint_status=200
bad_enum:      schema_errors=["'production' is not one of ...]"] endpoint_status=422
```

`"replicas": "3"` passed the endpoint and failed the schema the endpoint publishes. The cause is
pydantic's default lax mode, which coerces a numeric string to an integer. This is the same
coercion I hit on Day 3 with the standalone probe, except now it had become a contract bug: a
caller who codes against the published schema would reject payloads the API accepts.

Fixed by putting the model in strict mode, `ConfigDict(extra="forbid", strict=True)`.

The fix then appeared not to work. `"replicas": "3"` still returned `200` from the running server,
while calling `Intent.model_validate` directly in a shell rejected it with `int_type`. The server
was running stale code: `uvicorn --reload` never logged a reload for the edit, so the worker still
held the pre-strict model. Restarting uvicorn without `--reload` gave the expected `422`, and the
cross-check then agreed on every case. Two minutes were spent doubting the fix rather than the
reloader.

Strict mode also changed the wrong-type error `type` from `int_parsing` to `int_type`, which broke
the test asserting the old value. The test now asserts `int_type`, and a separate test pins the
`"3"` case so the coercion cannot come back unnoticed.

## Not here yet

- **Deploying an intent (planned, M2).** A valid intent is validated and echoed. Nothing is sent to
  Helm and no cluster is touched. `make demo` still prints its M2 placeholder.
- **Persistence (planned, M2/M3).** Accepted intents are not stored, so there is no intent to
  reconcile against and no lifecycle state yet.
- **Authentication.** The endpoint is open. Out of scope for every milestone currently mapped.
- **Secret scanning in CI (planned, M5).** The leak-scrub for this page was done by hand against
  the checklist in `CONTRIBUTING.md`.

## Checks

`ruff check .` clean and `pytest` green locally (9 tests), then the same two commands on the pull
request through the repo's one CI job, `lint-test`.
