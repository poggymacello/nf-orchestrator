# ADR-0003: The intent schema is the API contract

- **Status:** Accepted
- **Date:** 2026-07-22

## Context

Everything downstream of the API starts from an intent: a JSON object saying what network function
to run, how many replicas, and in which environment. The deploy engine (planned, M2) and the
reconciler (planned, M3) both act on that object. If a malformed intent reaches them, the failure
surfaces late, inside Helm or inside Kubernetes, where the error message no longer mentions the
field the caller got wrong.

The contract therefore has to be enforced at the edge, and it has to be published in a form a
caller can read without reading the source.

## Decision

A single pydantic model, `Intent` in [`orchestrator/intent.py`](../../../orchestrator/intent.py),
is the definition of the contract. Nothing else defines intent fields; the probe script
`scripts/intent_validate.py` imports the same model.

The model declares three required fields and forbids unknown ones:

| Field | Type | Constraint |
|---|---|---|
| `name` | string | matches `^[a-z0-9]([-a-z0-9]*[a-z0-9])?$`, max 63 characters |
| `replicas` | integer | 1 to 10 inclusive |
| `environment` | string | one of `dev`, `staging`, `prod` |

`name` follows the RFC 1123 label rules because it becomes a Helm release name at M2, and a name
that is legal here but illegal to Kubernetes would move the rejection past the API boundary.

Two routes expose it:

- `POST /intents/validate` returns `200` with `{"valid": true, "intent": {...}}` on a payload that
  satisfies the model.
- `GET /intents/schema` returns the JSON Schema generated from the model by
  `Intent.model_json_schema()`, so the published schema cannot drift from the enforcing code.

**One validator, not two.** Pydantic does the checking; the JSON Schema is derived output, not an
independently maintained file fed to a `jsonschema` validator. Keeping two validators in a repo
means keeping them in agreement.

The model runs in pydantic strict mode (`ConfigDict(strict=True, extra="forbid")`). Strict mode is
part of the decision, not an implementation detail: without it pydantic coerces a JSON string into
an integer, so `"replicas": "3"` is accepted by the endpoint while the schema published at
`/intents/schema` rejects it. See the build log for the run that exposed this.

**Error shape.** Rejections use FastAPI's default `RequestValidationError` handler. No custom
handler, no error envelope of our own. A rejection is HTTP `422` with a `detail` array, one entry
per violated field, each carrying `type`, `loc`, `msg`, and `input`. `loc` is prefixed with
`"body"` because the intent arrives as the request body. Callers branch on the machine-readable
`type` (`missing`, `int_type`, `literal_error`, `string_pattern_mismatch`, `extra_forbidden`)
rather than parsing `msg`.

## Verified against the running endpoint on 2026-07-22

Run with `uvicorn orchestrator:app --port 8000` and exercised with curl.

Valid payload, `HTTP 200`:

```json
{"valid":true,"intent":{"name":"sample-nf","replicas":2,"environment":"dev"}}
```

Enum violation (`"environment": "production"`), `HTTP 422`:

```json
{"detail":[{"type":"literal_error","loc":["body","environment"],"msg":"Input should be 'dev', 'staging' or 'prod'","input":"production","ctx":{"expected":"'dev', 'staging' or 'prod'"}}]}
```

The full set of captured requests and responses, including the missing-field and wrong-type cases,
is in [`docs/build-log/m1-intent-validation.md`](../../build-log/m1-intent-validation.md).

## Consequences

- **Good:** a caller gets a field-level reason for every rejection at the API boundary, before any
  cluster state is touched. The schema a caller reads is generated from the code that enforces it.
- **Good:** the error `type` values are stable identifiers, so a client can handle a missing field
  differently from a bad enum without string matching.
- **Cost:** the error body is pydantic's shape, so the contract is coupled to pydantic's error
  vocabulary. Changing validator later would change the response shape for every consumer.
- **Cost:** strict mode rejects payloads that a lenient caller might consider fine, such as
  `"replicas": "3"`. That is the intended trade, but it makes the contract stricter than most JSON
  APIs callers will have met.
- **Constraint:** any new intent field is a change to this ADR and to the published schema.

## Alternatives considered

- **A hand-written JSON Schema file validated with `jsonschema`.** Rejected: two artifacts to keep
  in agreement, and the endpoint would need its own code to map schema errors onto HTTP responses.
  Generating the schema from the model removes the drift.
- **A custom error envelope** (`{"error": {...}}`). Rejected: it hides the per-field detail that
  makes the rejection useful, and the default handler already produces a documented shape.
- **Accepting the intent and validating during deploy.** Rejected: the failure would surface inside
  Helm or Kubernetes, where the message no longer names the offending field.
- **Leaving pydantic in its default lax mode.** Rejected once measured: the endpoint and the
  published schema disagreed about `"replicas": "3"`.
