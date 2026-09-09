# Documentation

Engineering documentation for nf-orchestrator: the design decisions, build log, learning notes,
and operations material behind the code in this repository.

This is a **self-directed learning project**. Everything here is built from public upstream
sources and my own code and writing.

## What this covers

| Section | What you'll find |
|---|---|
| [Design](design/) | Problem statement, architecture diagram, NF lifecycle state model, and Architecture Decision Records (ADRs) |
| [Build log](build-log/) | An honest per-milestone journal: what I set out to do, what broke, and what I learned |
| [Daily log](daily-log/) | A dated day-by-day record of building this project |
| [Learning notes](learning-notes/) | Deep-dive notes on the tools I taught myself (FastAPI, JSON Schema, Helm/kind, Prometheus, GitHub Actions) |
| [Operations](operations/) | Runbooks, incident-management process, and blameless postmortems from deliberate failure drills |

## The system in one line

`intent JSON → schema validation → Helm deploy to kind → O-RAN-style lifecycle states → Prometheus/Grafana`,
with deliberate failure drills, blameless postmortems, and security gates (Gitleaks, Trivy) in CI.

## Status

M0 through M5 are done and M6 has its boundary; M7 is the publish pass this section is part of.
Documents are written after the code milestone they describe is real, and anything not yet built is
marked `(planned)` rather than described as if it exists. See
[`milestone-map.md`](milestone-map.md) for the mapping between code milestones and the documents
they produce, including the two things that are deliberately still missing.

## License

[MIT](../LICENSE).
