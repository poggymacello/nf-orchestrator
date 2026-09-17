# Postmortem — Drill 9: the API server sheds load

**Date:** 2026-09-17 · **Severity:** Sev-1 · **Status:** findings 1 and 2 fixed, 3 accepted with a
verified setting · **Follows:** [drill 8](2026-09-16-drill-8-a-slow-api-server.md), which imposed
latency and a queue with a proxy and left "the API server's own priority-and-fairness queues, which
shed load rather than queue it" undrilled

**Summary:** Kubernetes' API Priority and Fairness (APF) was configured to throttle the
orchestrator's identity, and a load generator on the same identity kept it saturated. The API server
rejected the excess with HTTP 429 and `Retry-After` up to 32 seconds, while answering `/readyz`
instantly. The orchestrator reported every one of those as **unreachable** — the same 503 and the
same `nf_cluster_reachable 0` as a dead control plane — so the page would have sent someone to
restart a control plane that was up and deliberately saying "not now".

## Hypotheses, written before the drill

Recorded at 11:03 local time:

- **H1.** kubectl and helm are client-go, which retries a 429 after its `Retry-After`. Under sustained
  shedding a call does not fail fast; it retries until the orchestrator's 3s bound kills it, and is
  reported as `ClusterUnreachable`.
- **H2.** If a 429 does surface as text, nothing classifies it, so it becomes HTTP 502 — which this
  API documents as "the cluster answered and refused the operation". A caller would not retry that.
- **H3.** In `/metrics`, throttled releases count as `reason="error"` and reachability stays 1, so
  nothing tells throttling apart from a broken release.

H1 was right, and worse than written. H2 did not arise inside the bound: client-go never gave up
within 3s. H3 was wrong — the first scrape reported the cluster **unreachable**, not merely erroring.

## Method

APF exempts `system:masters`, which is what kind's admin identity belongs to, so the orchestrator ran
as its own service account with read access to pods and secrets and nothing else. A FlowSchema routed
that account to a priority level with 3 seats, no borrowing, and `limitResponse: Reject`. The exact
objects are in [`manifests/apf-drill.yaml`](../manifests/apf-drill.yaml). A load generator made direct
HTTPS requests with the same account's token from 40 threads, so the only limit on its rate was the
API server's flow control.

```
apiserver_flowcontrol_current_limit_seats{priority_level="nf-drill-tight"} 3
load: {429: 886, 200: 6309} retry-after={'1': 27, '2': 208, '4': 263, '8': 388}
```

Real rejections from the real API server, with `Retry-After` climbing as the pressure continued — to
7, 14, 28 and then 32 seconds.

## Finding 1 — throttling was reported as an outage

With the load running, through the orchestrator:

```
/readyz                  200  0.22s {"status":"ready","cluster":"reachable"}
/deployments/apf-nf-3    503  3.52s {"detail":"kubectl get did not answer within 3s"}
/deployments/apf-nf-5    503  3.05s {"detail":"helm history did not answer within 3s"}
/metrics                 200  3.03s nf_cluster_reachable 0.0
/metrics                 200  3.31s nf_cluster_reachable 1.0 ...deadline 8.0 ...error 1.0
```

Three things are wrong at once. The status routes say the cluster did not answer, when it answered
immediately with a 429. The first scrape set `nf_cluster_reachable 0`, which is what
`ClusterUnreachable` pages on. And `/readyz` said "reachable" in the same second as everything else
said it was not.

Why, from kubectl at `-v=8`:

```
"Response" status="429 Too Many Requests" headers=< Retry-After: 32
"Got a Retry-After response" delay="32s" attempt=1 url=".../api/v1/namespaces/default/pods?labelSelector=app..."
```

client-go absorbs the 429 and sleeps for 32 seconds. Nothing is printed, nothing is returned, and the
orchestrator's bound — correctly — kills a command that has gone quiet. The information that would
have distinguished the two incidents never left the child process.

`/readyz` stayed fast because Kubernetes' built-in `probes` FlowSchema serves health endpoints outside
the ordinary priority levels. That turned out to be the way to tell them apart.

## Finding 2 — a surfaced 429 had no classification

`classify_error` knew "unreachable" and "conflict" wordings and nothing else. When client-go's
retries do run out, it prints `the server has received too many requests and has asked us to try
again later`, which would have become a plain `DeployError` — HTTP 502, "refused". Not observed
inside the bound in this drill, because with a 32s `Retry-After` the retries never run out in 3s;
found by reading the classification against the message client-go produces.

## Fix

**After a timed-out call, ask `/readyz`.** If the API server answers its readiness probe within
0.75s, the call raises a new `ClusterBusy` instead of `ClusterUnreachable`:

- HTTP **503 with `Retry-After: 5`**, and a detail that says the server is reachable and not serving
  this client. Still 503, because the orchestrator cannot answer; not 502, because nothing refused.
- `nf_cluster_busy 1`, `nf_cluster_reachable` stays 1, and throttled releases count as
  `reason="busy"`.
- A new alert, `ClusterThrottlingOrchestrator`, pages on `nf_cluster_busy == 1` for 2m;
  `ClusterUnreachable` no longer fires for it.

If `/readyz` does not answer either — a frozen or stalled server — nothing changes: it is still
unreachable. The readiness check itself is never probed a second time. client-go's 429 wording is
classified as busy for the case where it does surface. Reasoning:
[ADR-0016](../../design/adr/0016-busy-is-not-unreachable.md).

## After

Same load, fixed build:

```
/readyz                  200  0.14s {"status":"ready","cluster":"reachable"}
/deployments/apf-nf-3    503  3.27s "helm get did not answer within 3s, but the API server answers its
                                    readiness probe: it is reachable and not serving this client"
                                    [Retry-After: 5]
/metrics                 200  3.84s nf_cluster_reachable 1.0 nf_cluster_busy 1.0
                                    unreported: deadline 4, error 0, busy 3
/metrics                 200  3.17s nf_cluster_reachable 1.0 nf_cluster_busy 1.0
```

The second scrape could not even list releases, and said so without claiming the cluster was gone.
Against Prometheus with the repository's rules:

```
11:10:48 target up 3.42 | ClusterThrottlingOrchestrator=pending
11:12:57 target up 3.50 | ClusterThrottlingOrchestrator=firing
min nf_cluster_reachable over 3m: 1          (ClusterUnreachable never fired)
```

Regression against drill 7's server, which completes the TLS handshake and never answers:

```
stalled server  /readyz          503  3.03s  kubectl get did not answer within 3s   retry-after=None
stalled server  /deployments/x   503  3.81s  helm history did not answer within 3s  retry-after=None
stalled server  /metrics         200  3.81s  nf_cluster_reachable 0.0
```

Still unreachable. The price is visible in the second line: a timed-out call now takes up to 0.75s
longer, because the probe runs after it. A test pins read timeout plus probe inside the scrape budget.

## Finding 3 — the orchestrator throttled itself (accepted, with a verified setting)

When the load generator stopped, the busy signal did not clear. For more than two minutes it flipped
between 1 and 0, with 5 of 10 releases reported on the bad scrapes. The only client left was the
orchestrator:

```
in 60s with only the orchestrator: +11 rejected, +475 dispatched
```

Drill 8's waves grow to eight concurrent calls. This priority level had three seats. Every rejection
became a 32-second client-go retry, so that release missed the scrape. With the ceiling set below the
allowance:

```
NF_SCRAPE_WORKERS=2, about two minutes: +0 rejected, +1075 dispatched
busy=0  reported=10/10  scrape 0.84-0.97s   alerts: none
```

The three seats were this drill's choice, not a realistic allowance — service accounts normally land
in `workload-low`, which had 244 on this cluster. The default of eight stays. What changes is that
the runbook now says the ceiling has to sit below the orchestrator's APF seats, and how to read them.

## Action items

| # | Finding | Action | Status |
|---|---|---|---|
| 1 | A throttled cluster was reported as unreachable: 503 "did not answer", `nf_cluster_reachable 0` | Probe `/readyz` after a timeout; `ClusterBusy`, 503 + `Retry-After`, `nf_cluster_busy`, `ClusterThrottlingOrchestrator` | **fixed 2026-09-17** |
| 2 | client-go's 429 text had no classification and would read as a 502 refusal | Classify it as busy | **fixed 2026-09-17** |
| 3 | A concurrency ceiling above the identity's APF seats throttles the orchestrator by itself | Keep `NF_SCRAPE_WORKERS` below the seats; verified at 2 against 3; runbook | accepted |

## Reproduce

```bash
kind create cluster --name nf-drill --config kind-config.yaml
```

```bash
kubectl --context kind-nf-drill apply -f docs/operations/manifests/apf-drill.yaml
```

Run the orchestrator with a kubeconfig for the `nf-orchestrator` service account
(`kubectl create token nf-orchestrator`), saturate the same identity from another process, and read
`apiserver_flowcontrol_rejected_requests_total{priority_level="nf-drill-tight"}` from the API server's
`/metrics`. On Git Bash, set `MSYS_NO_PATHCONV=1` first, or `--raw /metrics` is rewritten into a
Windows path and answers `NotFound`.

What was **not** drilled: `limitResponse: Queue`, where APF holds requests instead of rejecting them
— the drill-8 shape, but with the server's own queues — and a cluster whose APF shares are
reconfigured while the orchestrator is running.
