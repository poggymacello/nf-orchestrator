# Runbook — cluster throttling the orchestrator

**Use when:** `ClusterThrottlingOrchestrator` is firing, `nf_cluster_busy` is 1, or a route returns
`503` with a `Retry-After` header and a detail saying the API server is *reachable and not serving
this client*.

**Severity:** Sev-1 while lifecycle series are missing — nothing can alert on those network
functions. It is **not** an outage: do not restart the control plane.

Every command here was run during
[drill 9](../postmortems/2026-09-17-drill-9-the-api-server-sheds-load.md) on 2026-09-17, against a
throwaway cluster whose API Priority and Fairness (APF) was configured to throttle the orchestrator.
Steps not exercised there are marked **(not drilled)**.

On Git Bash, set this first, or every `--raw /...` below is rewritten into a Windows path and the API
server answers `NotFound`:

```bash
export MSYS_NO_PATHCONV=1
```

## How this incident presents

The drill read these responses through the application's test client; the `curl` form is the
equivalent against a running server and was not itself run.

```bash
curl -s -i localhost:8000/deployments/<release> | grep -iE '^HTTP|retry-after|detail'
```

```
HTTP/1.1 503 Service Unavailable
retry-after: 5
{"detail":"helm get did not answer within 3s, but the API server answers its readiness probe:
 it is reachable and not serving this client (throttled or overloaded)"}
```

`/readyz` keeps answering `200` throughout — Kubernetes serves health endpoints outside the
throttled priority levels, which is exactly how the orchestrator tells this apart from an outage.
`ClusterUnreachable` does **not** fire. If it does, this is the wrong runbook:
[control plane unreachable](control-plane-unreachable.md).

## 1. Confirm the API server is shedding, and for whom

Rejections by priority level, from the API server itself (needs an identity that can read
`/metrics`):

```bash
kubectl get --raw /metrics | grep -E '^apiserver_flowcontrol_rejected_requests_total'
```

Run it twice a minute apart. A level whose count is climbing is the one shedding. Then find which
level the orchestrator's identity lands in:

```bash
kubectl get flowschemas
```

The drill's identity matched a FlowSchema named `nf-orchestrator` and landed in `nf-drill-tight`. By
default a service account matches the built-in `service-accounts` schema and lands in
`workload-low`.

## 2. Read that level's allowance

```bash
kubectl get --raw /metrics | grep -E '^apiserver_flowcontrol_(nominal|current)_limit_seats'
```

In the drill: `nominal 3`, `current 7` until borrowing was switched off, then `current 3`.
`workload-low` had 244 on the same cluster.

## 3. Is the orchestrator the one using it up?

If nothing else shares the identity and rejections keep climbing, the orchestrator is throttling
itself. Drill 9 measured exactly that after the external load stopped — 11 rejections a minute, with
`nf_cluster_busy` flipping between 1 and 0 and half the releases unreported — because the scrape
reads up to `NF_SCRAPE_WORKERS` releases at once (default 8) against a level with 3 seats.

**Keep `NF_SCRAPE_WORKERS` below the level's seats.** Restart the orchestrator with it set, then
repeat step 1:

```bash
NF_SCRAPE_WORKERS=2 uvicorn orchestrator:app --port 8000
```

Drill 9, 2 workers against 3 seats: zero rejections over two minutes, every release reported, scrapes
under a second.

## 4. If something else is using it

Another client sharing the orchestrator's identity, or its level, is starving it. The fix is on the
cluster's side — give the orchestrator its own identity, or its own FlowSchema and priority level
with enough seats. **(not drilled)** — the drill's load generator was removed, not re-homed.

## 5. Expect a tail after the pressure stops

`Retry-After` climbed to 32 seconds during the drill, and client-go waits it out before retrying.
Calls that were rejected just before the load stopped stay stuck for up to that long, so
`nf_cluster_busy` can stay at 1 after the cause is gone. The drill could not measure that tail on its
own: the orchestrator was throttling itself (step 3) and kept the signal at 1 for over two minutes.
With the ceiling lowered, the first scrape checked — 15 seconds after the restart — was already 0.
Wait at least a minute before concluding a fix did not work.

## What this runbook does not cover

- **`limitResponse: Queue`.** APF can hold requests instead of rejecting them; the orchestrator
  would then see slowness rather than 429s, closer to
  [drill 8](../postmortems/2026-09-16-drill-8-a-slow-api-server.md). **(not drilled)**
- **Changing APF configuration on a shared cluster.** The drill did it on a throwaway cluster.
  [`manifests/apf-drill.yaml`](../manifests/apf-drill.yaml) is not something to apply anywhere
  that matters.
