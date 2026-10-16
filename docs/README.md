# Documentation

Everything behind the code in this repository, organised by what you are trying to do.

This is a **self-directed learning project**, built from public upstream sources and its author's
own code and writing. Scope and non-goals: [overview](design/00-overview.md).

## Find what you need

| If you want to… | Read |
|---|---|
| Run it | The [quickstart](../README.md#quickstart), then the [configuration reference](reference/configuration.md) |
| Call the API, or handle its errors | [API reference](reference/api.md) |
| Scrape it, or route its alerts | [Metrics](reference/metrics.md) and [alerts](reference/alerts.md) |
| Respond to an alert | [Runbooks](operations/README.md#runbooks); every alert's `runbook_url` points at one |
| Understand how it works | [Architecture](design/architecture.md) and the [lifecycle state model](design/lifecycle-states.md) |
| Understand why it is built this way | [Architecture decision records](design/adr/) |
| See how it fails, and what was learned | [Postmortems](operations/README.md#drills-run-so-far) for all sixteen failure drills |
| Contribute | [CONTRIBUTING](../CONTRIBUTING.md) |
| Report a vulnerability | [SECURITY](../SECURITY.md) |

## Sections

| Section | Contents |
|---|---|
| [Reference](reference/) | Configuration, API and error model, metrics, alerts. Checked against the code in CI |
| [Design](design/) | Overview and non-goals, architecture, lifecycle states, ADRs |
| [Operations](operations/README.md) | Severity model, incident process, runbooks, postmortems, drill manifests |
| [Build log](build-log/) | Per milestone: what was set out to do, what broke, what was learned |
| [Daily log](daily-log/) | A dated record of every working day. History: not updated to match later code |
| [Learning notes](learning-notes/) | FastAPI, JSON Schema validation, Helm and kind, Prometheus instrumentation |
| [Milestone map](milestone-map.md) | Each code milestone, the documents it produced, and its status |

## Conventions

- A document describes what is true of the code at the commit it is in. What is not built is said to
  be not built.
- Runbook commands were run during a drill, with real output, or are marked **(not drilled)**.
- Postmortems are blameless, written from captured output, and state their hypotheses before the
  drill.

The full standards are in [CONTRIBUTING](../CONTRIBUTING.md#documentation-standards).
