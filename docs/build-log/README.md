# Build log

An honest per-milestone journal: what each milestone set out to do, what it actually shipped, what
broke, and what is still missing. Written **after** the milestone is real, from captured output
rather than from intent.

| Milestone | What it built |
|---|---|
| [M0](m0-scaffold.md) | Scaffold, FastAPI skeleton, kind cluster, one CI job — and three days of learning pydantic, Helm and Kubernetes by hand before automating any of it |
| [M1](m1-intent-validation.md) | The intent JSON Schema and the validation endpoint |
| [M2](m2-deploy-engine.md) | The deploy engine: a validated intent becomes a Helm release on kind |
| [M3](m3-reconciler.md) | The status reconciler, the four lifecycle states, and teardown |
| [M5](m5-ci-hardening.md) | CI hardening: the drills in CI, secret and private-term scanning, Trivy |

**M4 and M6 have no build log, deliberately.** M4's record is
[`../operations/`](../operations/README.md) — three failure drills, their postmortems and the
runbooks written from them — which is a fuller account than a summary would be. M6's is
[ADR-0012](../design/adr/0012-the-translator-is-untrusted-input.md),
[ADR-0013](../design/adr/0013-the-claude-translator-is-opt-in-and-unverified.md) and
[drill 4](../operations/postmortems/2026-09-09-drill-4-steering-the-translator-with-text.md), and
the milestone is not finished: the model-backed translator has never run against a live API.

The [daily log](../daily-log/) is the day-by-day version of the same story, and the
[milestone map](../milestone-map.md) says which documents each milestone owes.
