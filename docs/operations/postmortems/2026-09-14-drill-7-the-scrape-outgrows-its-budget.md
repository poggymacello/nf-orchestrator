# Postmortem — Drill 7: a stalled API server, and a scrape that outgrew its budget

**Date:** 2026-09-14 · **Severity:** Sev-1 · **Status:** findings 1-3 fixed, 4 accepted ·
**Follows:** [drill 6](2026-09-13-drill-6-frozen-control-plane.md), which left "an API server that
completes the TLS handshake and then stalls" undrilled

**Summary:** Drill 6's timeout held against the stall it was written for: 503 in three seconds, no
orphaned processes. The failure was somewhere else. That timeout was per subprocess call, and a
scrape makes four calls per release **in series**. On a cluster with nothing wrong at all, twenty
releases took a scrape to 8.4 seconds against Prometheus's 5, the target went down, and
`OrchestratorScrapeFailing` — the rule added yesterday — **paged that the orchestrator was down
while the orchestrator and the cluster were both healthy.**

## Hypotheses, written before the drill

Recorded at 11:30 local time, before anything was run:

- **H1.** Without drill 6's bound, kubectl and helm against a server that completes the handshake
  and never answers wait with no limit. With the bound: 503 in about 3s.
- **H2.** The 3s bound is per call, but a scrape makes `1 + ~4 × releases` calls in series, so "a
  cluster that is healthy but has several releases" pushes `/metrics` past the 5s scrape timeout.

H1 was right. H2 was right about the mechanism and wrong about "several": it took sixteen.

## Part 1 — the stall drill 6 could not produce

Drill 6 froze a node with `docker pause`, which stalls *before* the TLS handshake, so Go's 10s
handshake timeout ended it. Producing the other case needs a server that finishes the handshake.
It does not need a cluster: a 20-line Python TLS server that accepts, completes the handshake, reads
the request, and never answers, with a throwaway kubeconfig pointing at it
(`insecure-skip-tls-verify`, a dummy token).

This is a simulation at the network layer — no real API server was overloaded. What it reproduces
is the part that matters to the client: a connection that is fully established and silent.

Without any bound of ours, under an outer `timeout 120`:

```
11:31:15 handshake done, request: b'GET /Program%20Files/Git/readyz HTTP/1.1...'
11:33:15 handshake done, request: b'GET /version HTTP/1.1\r\nHost: 127.0.0.1:6444...'

kubectl --context stall get --raw=/readyz   rc=124   (killed by the outer timeout)
helm status x --kube-context stall          rc=124   (killed by the outer timeout)
```

Each tool held its connection for the full 120 seconds, and would have held it longer. (The mangled
path in the first request is Git Bash rewriting `/readyz` as a Windows path; it made no difference
to a server that answers nothing.)

With drill 6's bound, through the orchestrator:

```
/readyz            503  3.03s {"detail":"kubectl --context did not answer within 3s"}
/deployments/x     503  3.03s {"detail":"helm status did not answer within 3s"}
/metrics           200  3.03s ['nf_cluster_reachable 0.0']
kubectl/helm processes before=0 after=0
```

### Finding 1 — the timeout message named a flag

`kubectl --context did not answer`. The message took the command's second word, which for kubectl
is always `--context`. Cosmetic, but it is the one line an operator reads during the outage.

## Part 2 — the budget

Throwaway cluster `nf-drill`, releases added one at a time, three in-process scrapes at each count:

```
releases=1   scrape median=0.34s  max=0.38s
releases=2   scrape median=0.73s  max=0.74s
releases=4   scrape median=1.20s  max=1.42s
releases=6   scrape median=1.88s  max=2.00s
releases=8   scrape median=2.91s  max=3.28s
releases=12  scrape median=3.67s  max=4.19s
releases=16  scrape median=4.61s  max=5.02s
```

Linear, about a quarter to a third of a second per release on this host — Windows, where starting a
process is expensive, with kind under Docker Desktop. A Linux host would cross the line later. It
would still cross it.

### Finding 2 — a healthy cluster paged as an orchestrator outage

Twenty releases, the repository's own Prometheus config and rules, all pods `Running`:

```
direct /metrics from host: 200 8.37s

11:38:11 target: down 5.0 Get "http://host.docker.internal:8000/metrics": context deadline exceeded
  ... every scrape for 2.5 minutes ...
11:40:35 target: down 5.0 Get "http://host.docker.internal:8000/metrics": context deadline exceeded

nf_cluster_reachable series: 0
nf_deployment_state series:  0
ALERT OrchestratorScrapeFailing firing
```

Nothing was wrong. The page said the orchestrator was down, every lifecycle series was gone, and no
per-release alert could fire for any of the twenty releases. The same shape as drill 6 — correct
values computed and then thrown away by a caller with a shorter clock — except that here the input
was not an outage but growth.

Drill 6's fix did not cause this and could not have prevented it: it bounded each call, and none of
these calls was slow. Only their sum was.

### Finding 3 — past the budget, the same releases are invisible every time

The fix (below) lets a scrape stop at its budget and report what finished. To see that path on the
real stack, the orchestrator was run with `NF_SCRAPE_WORKERS=1`, which forces the serial 8s case:

```
direct /metrics: 200 4.21s  nf_releases_unreported{reason="deadline"} 7.0  state series: 52
11:46:04 target up 4.02  alerts: NFReleasesUnreported=pending
11:51:08 target up 4.02  alerts: NFReleasesUnreported=firing
```

The scrape stayed up, `OrchestratorScrapeFailing` never fired, and the count-based rule fired on its
five-minute window. Then the question the count did not answer — *which* seven?

```
releases ever reported in 6m: 15
never reported: ['load-nf-5', 'load-nf-6', 'load-nf-7', 'load-nf-8', 'load-nf-9']
helm list order: [..., 'load-nf-2', 'load-nf-3', 'load-nf-4', 'load-nf-5', ..., 'load-nf-9']
```

Helm lists releases alphabetically, so the scrape runs out of time at the same place every time and
the same five network functions never get a single series. If `load-nf-9` had failed, `NFFailed`
could not have fired. The only signal was a warning saying "7", naming nothing.

Rotating the order was considered and rejected: a series that comes and goes resets every `for:`
window, so each release would be seen sometimes and alerted on never.

## Fixes

**The scrape has one budget.** Releases are reconciled concurrently (`NF_SCRAPE_WORKERS`, default 8)
under a deadline for the whole scrape (`NF_SCRAPE_BUDGET`, default 4s). Work not finished by the
deadline is abandoned — queued reconciles are cancelled, running ones are already bounded by drill
6's per-call timeout — and counted in `nf_releases_unreported{reason="deadline"}`. A test pins
`SCRAPE_BUDGET < 5` and `READ_TIMEOUT <= SCRAPE_BUDGET`. Reasoning, and what was rejected:
[ADR-0014](../../design/adr/0014-the-scrape-has-one-budget.md).

**Every listed release says whether it was reported.** `nf_release_reported{release}` is 1 or 0 for
every name `helm list` returned, and `NFReleaseUnreported` fires per release on 0 for five minutes,
so the alert names the network function that has no alerting.

**The timeout message names the verb:** `kubectl get did not answer within 3s`.

## After

Twenty releases, default settings:

```
direct /metrics: 200 1.76s  nf_releases_unreported{reason="deadline"} 0.0  state series: 80
direct /metrics: 200 2.23s
direct /metrics: 200 1.67s
target up, lastScrapeDuration 1.33s-2.06s over 90s
INSTANTIATED releases in Prometheus: 20
no alerts
```

And forced over budget with one worker, after the per-release rule:

```
target up 4.01
NFReleaseUnreported firing  x5: load-nf-5, load-nf-6, load-nf-7, load-nf-8, load-nf-9
NFReleaseUnreported pending x1: load-nf-4
```

### Finding 4 — the release on the boundary (accepted)

`load-nf-4` sits where the deadline falls. Some scrapes reach it and some do not, so its series come
and go, and neither its lifecycle alerts nor `NFReleaseUnreported` hold for a full window. It is
still visible in `nf_releases_unreported`, and it only exists while the scrape is over budget — a
state the named alerts for its neighbours are already reporting. Accepted rather than engineered
around.

## Action items

| # | Finding | Action | Status |
|---|---|---|---|
| 1 | Timeout message named `--context` | Name the verb | **fixed 2026-09-14** |
| 2 | Scrape time grows with release count; past 5s a healthy cluster pages as an orchestrator outage | One budget for the whole scrape, releases reconciled concurrently | **fixed 2026-09-14** |
| 3 | Past the budget the same releases are never reported, and nothing names them | `nf_release_reported` per release, `NFReleaseUnreported` per release | **fixed 2026-09-14** |
| 4 | A release on the deadline boundary flaps and holds no alert window | Visible in the count; accepted | accepted |

## Reproduce

Part 1 needs only a TLS server that never answers and a kubeconfig pointing at it. Part 2:

```bash
kind create cluster --name nf-drill --config kind-config.yaml
```

Deploy twenty intents with `NF_KUBE_CONTEXT=kind-nf-drill`, run the orchestrator and Prometheus with
the files in `monitoring/`, and read the target's `lastScrapeDuration`. Set `NF_SCRAPE_WORKERS=1`
to force the over-budget path. Clean up with `kind delete cluster --name nf-drill`.

What was **not** drilled: a real API server under load, where every call is slow rather than one
connection being silent. The budget does not care which — it bounds the sum either way — but the
concurrency does: eight parallel reads are more load on an API server that is already struggling.
