# Postmortem — Drill 6: a frozen control plane

**Date:** 2026-09-13 · **Severity:** Sev-1 · **Status:** findings 1-3 fixed ·
**Closes:** the cost [ADR-0007](../../design/adr/0007-stability-is-an-alerting-concern.md) recorded
as "nothing times these subprocesses out yet"

**Summary:** Drill 2 stopped the control plane, so connections were refused and failed in under a
second. This drill froze it instead: `docker pause` keeps the port open and answers nothing. The
orchestrator came through with correct 503s in about ten seconds — but only because of a default
inside kubectl and helm, not anything in this project. Those ten seconds were twice Prometheus's
scrape timeout, so **the `ClusterUnreachable` alert had no data at all during the exact outage it
was written for.**

## Hypothesis, written before the drill

No helm or kubectl call has a timeout, so with the API server frozen the status endpoint and the
metrics scrape would hang until kubectl or helm gave up, possibly indefinitely.

**It was wrong**, and finding out why it was wrong was the drill.

## Timeline

Throwaway cluster `nf-drill`, orchestrator pointed at it with `NF_KUBE_CONTEXT`, one release
`INSTANTIATED`. Baseline: every route under 0.4s.

```
14:48:27  docker pause nf-drill-control-plane

  /healthz                 200   0.003s
  /readyz                  503  10.08s
  /deployments/frozen-nf   503  10.07s
  /metrics                 200  10.08s    nf_cluster_reachable 0.0
```

## Finding 1 — the wait was bounded by accident

Nothing hung. The 503s were correct. The error said why:

```
{"detail":"Error: kubernetes cluster unreachable: Get \"https://127.0.0.1:52086/version\":
           net/http: TLS handshake timeout"}
```

That is Go's `net/http` default: the TLS handshake gives up after ten seconds. `docker pause` lets
docker's proxy accept the TCP connection, the handshake never completes, and the client library
inside kubectl and helm abandons it. **The only timeout in play belonged to the tools this project
shells out to, and only applied because this failure happened to stall before the handshake.** An
API server that completes the handshake and then stalls — overloaded, deadlocked, stuck in a slow
admission webhook — has no such limit, and `subprocess.run` had no `timeout`.

This is drill 4's lesson in a new place: the gate held, and it held for a reason nobody designed.

## Finding 2 — the outage alert could not see the outage

Ten seconds per `/metrics` scrape would be fine if nothing else were counting. Prometheus was. The
repository's own config scrapes every 5s, and an unset `scrape_timeout` is clamped to the interval.
Run against the frozen cluster with that config:

```
effective scrape settings:  interval 5s  timeout 5s
target:  health=down  lastScrapeDuration=5.00s
         lastError='Get "http://host.docker.internal:8000/metrics": context deadline exceeded'
nf_cluster_reachable:  NO SERIES
up:  0
```

The orchestrator was correctly computing `nf_cluster_reachable 0`, and Prometheus abandoned the
scrape before receiving it. `ClusterUnreachable` fires on `nf_cluster_reachable == 0`, and a series
that does not exist is not equal to zero — so the alert written for a cluster-reachability outage
was structurally unable to fire during a cluster-reachability outage that happened to be slow.

## Finding 3 — every lifecycle alert was silent whenever the scrape failed

Once the orchestrator itself was stopped, the other half appeared:

```
up: 0
ClusterUnreachable          inactive
OrchestratorScrapeFailing   (did not exist)
```

`ClusterUnreachable` went from firing to **inactive** when the orchestrator died, because its series
went stale. The outage alert *resolved* at the moment things got worse. Every rule in
`nf-lifecycle.rules.yml` reads a metric the orchestrator emits, so every one of them depends on the
scrape succeeding, and there was no rule for the case where it does not.

## Fixes

**Every helm and kubectl call is bounded** — `run_bounded` passes a `timeout` to `subprocess.run` and
turns `TimeoutExpired` into `ClusterUnreachable`, so it becomes the same 503 as drill 2's refused
connection. Reads get `NF_READ_TIMEOUT` (3s) and writes `NF_WRITE_TIMEOUT` (60s), chosen by the helm
verb, so no call site changed. The read bound is deliberately inside the scrape timeout, and a test
pins `READ_TIMEOUT < 5` so the two cannot drift apart silently.

**A rule for the scrape itself.** `OrchestratorScrapeFailing`: `up{job="nf-orchestrator"} == 0` for
1m. It covers both the orchestrator being down and `/metrics` being slower than the timeout.

**`scrape_timeout: 5s` is stated in `prometheus.yml`** rather than inherited, since the read bound is
chosen against it.

## After

Same frozen cluster, fixed build:

```
  /readyz                  503  3.03s   {"detail":"helm status did not answer within 3s"}
  /deployments/frozen-nf   503  3.02s
  /metrics                 200  3.01s

target:  health=up  lastScrapeDuration=3.03s
nf_cluster_reachable:  0
ClusterUnreachable          firing
OrchestratorScrapeFailing   inactive
```

And with the orchestrator stopped, to see the new rule fire rather than assume it would:

```
up: 0
ClusterUnreachable          inactive
OrchestratorScrapeFailing   firing
```

Each alert fires for its own outage and not for the other's.

## What broke during the fix

The first re-run showed the orchestrator fixed and Prometheus returning nothing. It had exited on
restart: `yaml: invalid leading UTF-8 octet`. Adding the `scrape_timeout` line through Python's
`write_text()` on Windows used the platform encoding, cp1252, and the em dash in the new comment was
written as byte `0x97`. The check I had run to confirm the edit — `yaml.safe_load(open(...))` —
opened the file with the same default encoding, decoded the same byte the same way, and reported the
file valid. **A check that shares the writer's assumptions cannot find the writer's bug.**

Re-encoded the file, and added `test_committed_config_and_docs_are_valid_utf8`, which decodes
strictly. Confirmed it fails against a cp1252 probe file before trusting that it passes.

## Action items

| # | Finding | Action | Status |
|---|---|---|---|
| 1 | Cluster calls bounded only by Go's TLS handshake default | Bound every call; timeout becomes `ClusterUnreachable` | **fixed 2026-09-13** |
| 2 | `/metrics` slower than the scrape timeout during an outage | Read bound inside the scrape timeout, pinned by a test; `scrape_timeout` stated | **fixed 2026-09-13** |
| 3 | No alert when the scrape itself fails | `OrchestratorScrapeFailing` on `up == 0`, verified firing | **fixed 2026-09-13** |

## Reproduce

```bash
kind create cluster --name nf-drill --config kind-config.yaml
```

```bash
NF_KUBE_CONTEXT=kind-nf-drill uvicorn orchestrator:app --port 8000
```

```bash
docker pause nf-drill-control-plane
```

Time `GET /readyz`, `GET /metrics`, and read the Prometheus target's `lastScrapeDuration`. Recover
with `docker unpause nf-drill-control-plane`, then `kind delete cluster --name nf-drill`.

What was **not** drilled: an API server that completes the TLS handshake and then stalls. That is
the case finding 1 argues the timeout is really for, and producing it reproducibly needs a proxy in
front of the API server that this drill did not build.
