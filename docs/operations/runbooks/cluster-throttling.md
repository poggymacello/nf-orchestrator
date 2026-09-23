# Runbook — cluster throttling the orchestrator

**Use when:** `ClusterThrottlingOrchestrator` is firing, `nf_cluster_busy` is 1,
`nf_cluster_probe_seconds` is high, or a route returns `503` with a `Retry-After` header and a
detail saying the API server is *reachable and not serving this client*.

**Severity:** Sev-1 while lifecycle series are missing — nothing can alert on those network
functions. It is **not** an outage: do not restart the control plane.

Every command here was run during
[drill 9](../postmortems/2026-09-17-drill-9-the-api-server-sheds-load.md) on 2026-09-17 and
[drill 10](../postmortems/2026-09-23-drill-10-the-api-server-queues-instead-of-refusing.md) on
2026-09-23, against throwaway clusters whose API Priority and Fairness (APF) was configured to
throttle the orchestrator — rejecting in drill 9, queueing in drill 10. Steps not exercised there
are marked **(not drilled)**.

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
throttled priority levels, which is how the orchestrator tells this apart from an outage.
`ClusterUnreachable` does **not** fire. If it does, this is the wrong runbook:
[control plane unreachable](control-plane-unreachable.md).

**The quiet form of this incident has no `503` at all.** When APF queues instead of rejecting
(drill 10), every call succeeds eventually and the only symptoms are a scrape near its budget,
releases missing for `reason="deadline"`, and a slow probe:

```bash
curl -s localhost:8000/metrics | grep -E '^nf_(cluster_busy|cluster_probe_seconds|releases_unreported)'
```

```
nf_cluster_busy 1.0
nf_cluster_probe_seconds 0.765
nf_releases_unreported{reason="deadline"} 5.0
```

`nf_cluster_probe_seconds` is how long the API server took to answer `/readyz`, the cheapest call it
serves: 0.06 – 0.25s idle in the drill, 0.25 – 0.80s while queueing, and up to 3.58s measured from a
shell. Above 0.5s the orchestrator calls the cluster busy. If that number is normal and releases are
still late, the scrape really is short of time — go to
[scrape over budget](scrape-over-budget.md).

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

## 6. Check which shape of throttling it is

```bash
kubectl get prioritylevelconfiguration <level> -o jsonpath='{.spec.limited.limitResponse.type}'
```

`Reject` is drill 9: 429s, `Retry-After` from the server, `nf_releases_unreported{reason="busy"}`.
`Queue` is drill 10: no errors at all, releases late for `reason="deadline"`, and the probe duration
carrying the signal on its own. A queue that overflows rejects as well, so both can appear at once —
drill 10 saw 210 rejections with `Retry-After` 1, 2, 4 and 8 seconds before its queue was widened.

Queueing also reaches further than the orchestrator's own reads: a deploy waits in the same queue,
and `Retry-After: 5` on a `503` is the orchestrator's guess, not the cluster's, because a queueing
server publishes no estimate of the wait.

## What this runbook does not cover

- **Changing APF configuration on a shared cluster.** The drills did it on throwaway clusters.
  [`manifests/apf-drill.yaml`](../manifests/apf-drill.yaml) and
  [`manifests/apf-queue-drill.yaml`](../manifests/apf-queue-drill.yaml) are not something to apply
  anywhere that matters.
- **Separating a slow cluster from a loaded orchestrator host.** `nf_cluster_probe_seconds` includes
  the cost of starting kubectl locally, so a machine under heavy load inflates it. Both readings
  mean "the orchestrator is not being served promptly"; deciding which end is at fault needs the API
  server's own latency metrics. **(not drilled)**
