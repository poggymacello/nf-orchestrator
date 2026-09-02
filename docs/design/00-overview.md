# Overview

nf-orchestrator takes a declarative intent (a JSON document describing a network function to
deploy) and turns it into a running Kubernetes workload, reporting lifecycle state and metrics
along the way. It exists to show, end to end and on a laptop, the pattern behind SMO-driven network
function orchestration in O-RAN: a validated intent goes in, a deployed and observable workload
comes out.

## Goals

- Validate an intent against a schema before it can touch a cluster.
- Deploy the resulting workload with Helm onto a local `kind` cluster.
- Report deployment state through a small, well-defined lifecycle enum
  (`NOT_INSTANTIATED` / `INSTANTIATING` / `INSTANTIATED` / `FAILED`), not raw pod phases.
- Expose metrics (deployments total, failures, time-to-instantiate) and survive deliberate failure
  drills with documented runbooks and postmortems.

## Non-goals

- **Not a production O-Cloud.** No high availability, no multi-tenant isolation, no upgrade path
  for a live cluster. This runs on one laptop against one `kind` cluster.
- **Not tied to any radio unit or vendor hardware.** The deployed workload is a generic
  stand-in network function; nothing here talks to physical RF equipment.
- **Single-cluster only.** No multi-cluster federation, no cross-cluster scheduling. One intent
  targets the one `kind` cluster this project runs against.
- **The LLM intent layer is out of the core loop.** Text-to-intent translation is milestone M6,
  bolted on in front of the validated-JSON boundary. The core loop (validate → deploy → report)
  works without it and never depends on it — the LLM only ever produces a JSON document that goes
  through the same schema validation as any other intent.
- **Not a general-purpose Kubernetes platform.** No ingress management, no service mesh, no
  multi-namespace tenancy model. The scope stops at deploying and tracking one kind of workload.

## Where this stands today

The whole intent → deploy → lifecycle → metrics path is wired and running (M0 through M4). An intent
is validated, deployed with Helm to `kind`, and its lifecycle state derived from live cluster
signals on every read, with Prometheus gauges and alerting rules over the top.

Three deliberate failure drills have been run against a real cluster, with postmortems, runbooks,
and fixes verified by re-running the drill — see [`../operations/`](../operations/README.md). Every
one of them found the orchestrator reporting something false, and the fixes are recorded as
ADR-0006 through ADR-0009.

M5 is in progress: the end-to-end lifecycle now runs in CI against a real kind cluster on every
push. Gitleaks, Trivy and the banned-terms grep are still `(planned)`, so the leak-scrub in
`CONTRIBUTING.md` remains a manual step. The LLM intent layer is M6 and does not exist.

See [`milestone-map.md`](../milestone-map.md) for what maps to which code milestone.
