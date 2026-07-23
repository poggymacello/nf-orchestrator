# Milestone map — code → docs

This document maps each code milestone to the documentation it produces. Status reflects what is
actually built, not what is planned.

| Code milestone | What it builds | Documents produced here | Status |
|---|---|---|---|
| **M0** | Scaffold, FastAPI skeleton, kind up, basic CI (lint+test) | build-log/m0, ADR-0002 (FastAPI) | code: done · docs: in progress |
| **M1** | Intent JSON Schema + validation endpoint | build-log/m1, ADR-0003 (schema contract), learning-notes/json-schema | code: done · docs: done |
| **M2** | Deploy engine — intent deploys stand-in NF via Helm to kind | build-log/m2, ADR-0004 (Helm), learning-notes/helm-and-kind | code: done · docs: done |
| **M3** | Status reconciler + lifecycle states + teardown | build-log/m3, design/lifecycle-states, ADR-0005 | code: done · docs: done |
| **M4** | Observability + 3 failure drills + postmortems + runbooks | operations/runbooks, operations/incidents, operations/postmortems | planned |
| **M5** | CI hardening — Gitleaks, Trivy, e2e with kind | docs on CI security gates | planned |
| **M6** | LLM intent layer (text → validated JSON) with guardrails | ADR-0006 (LLM guardrail) | planned |
| **M7** | README polish, architecture + lifecycle diagrams, quickstart | design/overview, design/architecture, publish pass | planned |

## Rule

A document is written **after** its code milestone is real. Until then it stays `planned` here and
is not published as if complete.
