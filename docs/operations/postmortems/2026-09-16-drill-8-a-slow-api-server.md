# Postmortem — Drill 8: an API server that is slow, not silent

**Date:** 2026-09-16 · **Severity:** Sev-1 · **Status:** finding 1 fixed, 2 and 3 accepted ·
**Closes:** the cost [ADR-0014](../../design/adr/0014-the-scrape-has-one-budget.md) recorded as
"eight parallel reads are more load on an API server that is already struggling. Not drilled"

**Summary:** Drills 6 and 7 used a control plane that was frozen or absent. This one is the case in
between and the common one in production: a cluster that still answers, slowly, and serves only so
much at a time. The scrape budget held — Prometheus never saw a failed scrape — but the concurrency
added on day 25 turned out to be **actively harmful** under that load. Sixteen workers left all
thirty releases half-read and **none** reported. Two workers reported six. The fix was to stop
starting work the budget cannot finish.

## Hypotheses, written before the drill

Recorded at 10:31 local time:

- **H1.** With a constant added latency L, the scrape is `(2 + 2N)` calls over `W` workers, so 30
  releases at L=0.3s and W=8 would land around 2.3s and lose nothing.
- **H2.** Against a server with limited concurrency, raising `W` buys nothing, and queued calls age
  into the 3s per-call timeout, so **more workers means more timeouts**.
- **H3.** The budget still protects: `/metrics` answers, Prometheus stays up, and losses show up in
  `nf_releases_unreported`.

H1 was wrong by a factor of two. H2 was wrong about the mechanism and right that concurrency hurts.
H3 held exactly.

## Method

A TCP proxy in front of the kind API server, with two knobs: `LATENCY` (delay before each connection
is served) and `CAPACITY` (how many connections are in flight at once; the rest queue). Bytes are
relayed untouched, so TLS stays end to end and the cluster's own CA verifies — only the timing
changes. A copy of the kubeconfig points at the proxy's port.

One `kubectl get pods` direct: 182ms. Through the proxy at LATENCY=0.3: 521ms.

## Finding 1 — under load, every extra worker made the answer worse

Thirty releases, 4s budget, `LATENCY=0.3 CAPACITY=2` — slow *and* limited, which is what an
overloaded API server is:

```
workers=1    median 4.00s   reported  4/30   deadline 26
workers=2    median 4.02s   reported  8/30   deadline 22
workers=8    median 4.00s   reported  4/30   deadline 26
workers=16   median 4.02s   reported  0/30   deadline 30     proxy: peak_queue=15 max_wait=2.83s
```

Read from the cluster's side instead of the collector's, the same shape appears:

```
workers= 2:  6 releases finished inside 4s
workers= 8:  8 releases finished inside 4s
workers=16:  0 releases finished inside 4s
```

The mechanism is not timeouts — **no read timed out at all**, `error` stayed 0. Throughput is fixed,
a release needs two calls in series, and its second call queues behind the first call of every other
release in flight. Thirty in flight over a fixed queue means everyone advances and nobody arrives.
Day 25 chose concurrency to fit more releases inside the budget, which is right when the cluster has
capacity to spare and precisely wrong when it does not.

An intermediate run separates the two knobs. With `CAPACITY=2` but only `LATENCY=0.05`, eight
workers were *fine* — 2.53s, all 30 reported. A queue alone is not the problem; a queue whose
service time is long is. And with latency alone (`CAPACITY=1000, LATENCY=0.3`), eight workers still
lost 6 of 30, because the two calls per release are serial: the added latency lands twice per
release, which is what H1's arithmetic missed.

## Finding 2 — the budget held, and said so (accepted, no change)

Throughout, `/metrics` answered inside the budget, the Prometheus target stayed `up`, and
`OrchestratorScrapeFailing` never fired. End to end at 60 releases against the overloaded server,
after the fix:

```
target up, lastScrapeDuration 2.86-2.96s over five minutes
reported whole: 6 of 60
nf_releases_unreported{reason="deadline"} 54
NFReleaseUnreported firing x54, naming load-nf-06, load-nf-07, load-nf-08, ...
```

Drill 7's rules did their job: the operator is told the cluster is reachable, the scrape is healthy,
and 54 named network functions have no coverage. That is the honest report of a cluster that cannot
answer fast enough, and it is what the runbook is for.

## Finding 3 — the concurrency that helps is cluster-specific (accepted)

`NF_SCRAPE_WORKERS=2` beat 8 and 16 under load; 8 beat 2 on a healthy cluster. There is no single
right number, which is why the fix below does not pick one — it measures.

## Fix

**Admit work in waves.** The collector starts with a wave of 2 releases, doubles the wave while a
wave completes, and stops admitting once the remaining budget is shorter than the last wave took.
`NF_SCRAPE_WORKERS` becomes a ceiling rather than a target. Reasoning and rejected alternatives:
[ADR-0015](../../design/adr/0015-admit-scrape-work-in-waves.md).

## After

Same overloaded server, 30 releases:

```
waves, workers=8    median 2.94s   reported [6, 6, 6] of 30
waves, workers=16   median 2.91s   reported [6, 6, 6] of 30
```

Stable at six whichever ceiling is set, where before it was four and zero — and the scrape now ends
early instead of spending the whole budget on work it cannot finish. On a healthy cluster there is
no regression: 30 releases in 1.26s, and 60 in 1.92s with all 60 reported, against 2.05s the day
before.

## Action items

| # | Finding | Action | Status |
|---|---|---|---|
| 1 | Concurrency against a saturated API server finished fewer releases, and none at 16 workers | Admit work in waves, sized by measured cost | **fixed 2026-09-16** |
| 2 | Under sustained overload most releases stay unreported | None — the budget, the count and the per-release alert already report it truthfully | accepted |
| 3 | The best worker count differs per cluster | None — the wave size is measured per scrape rather than configured | accepted |

## Reproduce

```bash
kind create cluster --name nf-drill --config kind-config.yaml
```

Deploy N intents, then put the proxy in front of the API server and point a copy of the kubeconfig
at it:

```bash
LATENCY=0.3 CAPACITY=2 python slow_api_proxy.py <api-port> 18443
```

Scrape with `NF_SCRAPE_WORKERS` at 1, 2, 8 and 16 and compare `nf_release_reported`.

What was **not** drilled: a real API server saturated by real traffic, rather than a proxy imposing
latency and a queue. The proxy models the timing an overloaded server produces, not its causes —
etcd contention, a slow admission webhook, or the API server's own priority-and-fairness queues,
which would shed load rather than queue it indefinitely.
