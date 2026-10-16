# Reference

Exact, current descriptions of the orchestrator's interfaces. Each page is checked against the code
by `tests/test_reference_docs.py`, so a change that makes one of them false fails CI.

| Page | Covers |
|---|---|
| [Configuration](configuration.md) | Every environment variable, its default and the decision behind it |
| [API](api.md) | Routes, the intent schema, response shapes, and the error model: which status means whose failure, and whether to retry |
| [Metrics](metrics.md) | Every series `/metrics` exposes, with type, labels and meaning |
| [Alerts](alerts.md) | Every alert rule's severity, hold time, expression and runbook |

For the reasoning behind these interfaces, see [design](../design/); for responding to what they
report, see [operations](../operations/README.md).
