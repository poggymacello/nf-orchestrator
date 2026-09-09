# Milestone map — code → docs

This document maps each code milestone to the documentation it produces. Status reflects what is
actually built, not what is planned.

| Code milestone | What it builds | Documents produced here | Status |
|---|---|---|---|
| **M0** | Scaffold, FastAPI skeleton, kind up, basic CI (lint+test) | build-log/m0, ADR-0002 (FastAPI) | code: done · docs: in progress |
| **M1** | Intent JSON Schema + validation endpoint | build-log/m1, ADR-0003 (schema contract), learning-notes/json-schema | code: done · docs: done |
| **M2** | Deploy engine — intent deploys stand-in NF via Helm to kind | build-log/m2, ADR-0004 (Helm), learning-notes/helm-and-kind | code: done · docs: done |
| **M3** | Status reconciler + lifecycle states + teardown | build-log/m3, design/lifecycle-states, ADR-0005 | code: done · docs: done |
| **M4** | Observability + 3 failure drills + postmortems + runbooks | learning-notes/prometheus-instrumentation, operations/runbooks, operations/postmortems, ADR-0006 to ADR-0009 | code: `/metrics`, lifecycle and operation gauges, reconciler fixes, `/readyz`, repair route done · 3 drills run, every defect they found is fixed, alert rules verified · **done** (one open item: the eviction threshold the abandoned disk drill would need) |
| **M5** | CI hardening — Gitleaks, Trivy, e2e with kind | build-log/m5, ADR-0010, ADR-0011 | **done** — five CI jobs on every branch push: lint-test, secret-scan, banned-terms, vuln-scan, e2e on a pinned Kubernetes matrix. The stand-in NF chart is hardened to 0 HIGH findings. `banned-terms` fails closed until the `BANNED_TERMS` secret is set |
| **M6** | LLM intent layer (text → validated JSON) with guardrails | ADR-0012, ADR-0013, operations/postmortems (drill 4) | boundary and guardrails: **done** — `POST /intents/translate` returns a candidate that goes through the same schema, and translating never deploys; drill 4 run and its findings fixed · model-backed translator: written and unit tested behind the same protocol, but **never run against a live API** (no key in the build environment) — see ADR-0013 |
| **M7** | README polish, architecture + lifecycle diagrams, quickstart | design/overview, design/architecture, publish pass | README, architecture and overview brought up to date early (2026-09-02), because the front-door docs still described the M0 skeleton · remaining publish pass: planned |

## Rule

A document is written **after** its code milestone is real. Until then it stays `planned` here and
is not published as if complete.
