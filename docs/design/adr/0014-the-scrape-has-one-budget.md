# ADR-0014: The scrape has one budget, and an unreported release is named

- **Status:** Accepted
- **Date:** 2026-09-14
- **Closes:** findings 2 and 3 of
  [drill 7](../../operations/postmortems/2026-09-14-drill-7-the-scrape-outgrows-its-budget.md)
- **Revisits:** the scrape cost recorded in
  [ADR-0007](0007-stability-is-an-alerting-concern.md)

## Context

ADR-0007 made Prometheus the poller: every scrape reconciles every release. It wrote down the cost —
"at a large release count or a tight interval it would not be [fine], and the fix then is a real
cache or a watch" — without saying where "large" was.

Drill 7 measured it. Each release costs four subprocess calls, run in series, about 0.3s on the
development host. Prometheus abandons a scrape at 5s. Sixteen releases touched the limit; twenty
took 8.4s, every scrape failed, every lifecycle series disappeared, and `OrchestratorScrapeFailing`
paged for an orchestrator that was up, over a cluster that was healthy.

Drill 6's per-call timeout could not help. It bounds a call that is slow; none of these were. The
failure was the sum.

## Decision

**Give the scrape a deadline, and spend it concurrently.**

- `LifecycleCollector` takes a deadline of `NF_SCRAPE_BUDGET` (default 4s) from the moment it starts,
  including the `helm list`.
- Releases are reconciled on a thread pool of `NF_SCRAPE_WORKERS` (default 8). The work is almost
  entirely waiting on subprocesses, so threads are enough.
- At the deadline the collector emits what finished. Queued reconciles are cancelled; running ones
  are left to end on their own, which drill 6's per-call timeout bounds.
- The budget is pinned below the scrape timeout, and the read timeout at or below the budget, by
  tests — the same way drill 6 pinned the read timeout.

**Report the gap, and name it.**

- `nf_releases_unreported{reason="deadline"|"error"}` counts releases that emitted no lifecycle
  series this scrape.
- `nf_release_reported{release}` is emitted for every release `helm list` returned, 1 or 0. It is
  cheap — the names are already known — and it exists precisely when the expensive series do not.
- `NFReleaseUnreported` fires per release on `nf_release_reported == 0` for 5m, severity `warn`.

Naming mattered more than counting. Past the budget it is the same releases that miss out every
scrape, because helm's order is stable; a count of seven said nothing about which network functions
had no alerting.

**State stays derived on read.** Nothing is cached, and every value a scrape emits was read from the
cluster during that scrape. ADR-0005 and ADR-0007 both still hold.

## Consequences

- **Good:** twenty releases scrape in 1.3-2.2s instead of 8.4s, and a scrape over budget stays `up`
  and says so, instead of failing whole and looking like an outage.
- **Good:** `OrchestratorScrapeFailing` goes back to meaning what its name says.
- **Good:** an NF with no alerting coverage is itself alerted on, by name.
- **Cost:** up to eight kubectl and helm processes at once. On a healthy API server that is nothing;
  on one that is already struggling it is eight times the read load of the serial version, arriving
  exactly when it hurts. Not drilled.
- **Cost:** a release on the deadline boundary is reported on some scrapes and not others, so no
  window-based alert holds for it, including `NFReleaseUnreported`. It shows in the count. Accepted
  (drill 7, finding 4).
- **Cost:** abandoned reconciles keep running for up to the read timeout after the scrape has
  answered, so a scrape that hits the deadline leaves some work overlapping the next one.
- **Cost:** this moves the ceiling, it does not remove it. Eight workers should fit several times
  more releases in the same budget on the same host; **that number was not measured**, and past it
  the named alerts are the signal that the fix below is due.

## Alternatives considered

- **A background reconcile loop feeding a cache, `/metrics` serving the cache.** This is ADR-0007's
  "real cache", and at a much larger scale it is the right answer. Rejected for now: it replaces
  "every value was read this scrape" with "every value was read at some point", which needs its own
  staleness metric and alert to stay honest, and it adds the process lifecycle ADR-0007 rejected. The
  measured problem was twenty releases on one kind cluster; concurrency fixes that without giving up
  derive-on-read.
- **A watch on Deployments and Pods.** Rejected for the same reason at greater cost: it turns the
  orchestrator into a controller with its own state to keep correct across restarts.
- **Raise Prometheus's scrape timeout.** Rejected: the timeout is set by whoever runs Prometheus, not
  by this project, and it would only move the release count at which the same failure recurs. The
  interval is 5s, so the timeout cannot exceed it anyway.
- **Rotate or shuffle the release order so every release is reported sometimes.** Rejected: a series
  that is present on some scrapes and stale on others resets every `for:` window, so each release
  would be visible occasionally and alerted on never. Naming the unreported releases is honest about
  the gap; rotating would hide it.
- **Fewer calls per release** (reading status, history and values in one helm call). Worth doing,
  and not done here: it reduces the slope, but the collector would still have no deadline, and a
  slope is what drill 7 showed eventually crosses a fixed line.
