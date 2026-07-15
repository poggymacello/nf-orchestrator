# JSON Schema / pydantic validation

An intent has to be rejected before it reaches the cluster, not after — a malformed deploy request
should fail as a 4xx response, not as a half-created Helm release.

## Mental model

Pydantic turns a Python class with type-annotated fields into both a validator and a JSON Schema.
Each field is a rule: a type, and optionally an allowed set of values (`Literal`). A payload is
checked against every field at once; every violation gets collected into a single error list rather
than stopping at the first one.

## The model used for this exercise

```python
from typing import Literal
from pydantic import BaseModel

class Intent(BaseModel):
    name: str
    replicas: int
    environment: Literal["dev", "staging", "prod"]
```

The `orchestrator/` app has no HTTP endpoint that uses this yet — the intent-submit endpoint is
`(planned, M1)`. This model was validated standalone with `Intent.model_validate(payload)`.

## Valid payload

```
{'name': 'sample-nf', 'replicas': 2, 'environment': 'dev'}
```
```
ACCEPTED: name='sample-nf' replicas=2 environment='dev'
```

## Invalid: required field missing

```
{'name': 'sample-nf', 'environment': 'dev'}
```
```json
[
  {
    "type": "missing",
    "loc": ["replicas"],
    "msg": "Field required",
    "input": {"name": "sample-nf", "environment": "dev"},
    "url": "https://errors.pydantic.dev/2.13/v/missing"
  }
]
```
Tells the caller exactly which field is absent (`loc`) and why (`missing`), with the full input
echoed back so the caller can see what they actually sent.

## Invalid: wrong type

```
{'name': 'sample-nf', 'replicas': 'two', 'environment': 'dev'}
```
```json
[
  {
    "type": "int_parsing",
    "loc": ["replicas"],
    "msg": "Input should be a valid integer, unable to parse string as an integer",
    "input": "two",
    "url": "https://errors.pydantic.dev/2.13/v/int_parsing"
  }
]
```
`int_parsing` is a distinct error type from `missing` — the field was present but unusable.

## Invalid: enum violation

```
{'name': 'sample-nf', 'replicas': 2, 'environment': 'production'}
```
```json
[
  {
    "type": "literal_error",
    "loc": ["environment"],
    "msg": "Input should be 'dev', 'staging' or 'prod'",
    "input": "production",
    "ctx": {"expected": "'dev', 'staging' or 'prod'"},
    "url": "https://errors.pydantic.dev/2.13/v/literal_error"
  }
]
```
The `ctx.expected` field spells out the exact allowed set, so the caller doesn't have to go read
the schema to find valid values.

## The gotcha: numeric strings get silently coerced

`"replicas": "two"` gets rejected (above), but `"replicas": "3"` does not:

```
$ python -c "from scripts.intent_validate import Intent; print(Intent.model_validate({'name': 'x', 'replicas': '3', 'environment': 'dev'}))"
name='x' replicas=3 environment='dev'
```

Pydantic v2's default mode coerces any string that can be parsed as an int, not just ones that are
already the right type. The boundary is "can this be parsed", not "is this already typed correctly".
A client sending `"replicas": "3"` as a string gets it silently accepted as `int(3)` — worth knowing
before assuming rejected input always means wrong-type input.

## Commands run

```
python scripts/intent_validate.py
```
Runs all four payloads (1 valid, 3 invalid) and prints the real accept/reject output for each,
exactly as pasted above.
