# ADR-0015: A scrape admits work in waves, and stops when the budget says so

- **Status:** Accepted
- **Date:** 2026-09-16
- **Closes:** finding 1 of
  [drill 8](../../operations/postmortems/2026-09-16-drill-8-a-slow-api-server.md)
- **Revisits:** the concurrency introduced by [ADR-0014](0014-the-scrape-has-one-budget.md)

## Context

ADR-0014 gave the scrape a deadline and spent it concurrently: every release submitted at once to a
pool of eight. It recorded the risk and did not test it — "eight parallel reads are more load on an
API server that is already struggling. Not drilled."

Drill 8 drilled it. Against a server that was slow *and* served a limited number of calls at a time,
concurrency stopped helping and started hurting: 2 workers reported 6 of 30 releases, 8 reported 4,
and 16 reported **none**. No read timed out; nothing failed. Throughput was fixed, a release needs
two calls in series, and its second call queues behind the first call of every other release in
flight. Everything advanced; nothing arrived.

Submitting every release at once is a bet that the cluster can serve them all. The bet is lost
exactly when monitoring matters most.

## Decision

**Admit work in waves, sized by evidence.**

- The first wave is 2 releases.
- After each wave completes, the wave size doubles, up to `NF_SCRAPE_WORKERS` (default 8).
- Before starting a wave, the collector compares the remaining budget with how long the last wave
  took. If there is not that much budget left, it stops admitting.
- Releases never admitted, and any still running at the deadline, are reported exactly as before:
  `nf_release_reported` 0, counted in `nf_releases_unreported{reason="deadline"}`.

`NF_SCRAPE_WORKERS` changes meaning: it is now a **ceiling** on concurrency, not a target. On a
healthy cluster the waves reach it within three waves and behave as before.

Nothing is cached and nothing is stored between scrapes. The measurement that sizes the waves lives
inside one scrape and dies with it, so ADR-0005's derive-on-read still holds.

## Consequences

- **Good:** under load the scrape returns whole answers about a few releases instead of partial
  answers about all of them. A partial answer emits no series at all, so "all of them" meant nothing.
- **Good:** stable across ceilings — 6 of 30 reported at both 8 and 16 workers, where the old code
  gave 4 and 0. The operator no longer has to guess a worker count per cluster.
- **Good:** an overloaded scrape now ends early (2.9s of a 4s budget) rather than spending the
  remainder on work that cannot finish, which also stops adding load to the struggling server.
- **Good:** no regression on a healthy cluster: 60 releases in 1.92s with all 60 reported, against
  2.05s before.
- **Cost:** waves add synchronisation points. A scrape waits for a wave to finish before starting
  the next, so one slow release delays the releases behind it in a way a flat pool would not.
- **Cost:** the doubling wastes part of the budget learning. At 60 healthy releases the first three
  waves (2, 4, 8) cost about a fifth of a second before the ceiling is reached.
- **Cost:** it is still first-come-first-served over helm's alphabetical order, so under sustained
  overload the same releases are reported and the same ones are not — drill 7's finding 3, unchanged.
  `NFReleaseUnreported` names them.

## Alternatives considered

- **Keep a flat pool and lower the default worker count.** Rejected: 2 was better under load and
  worse when healthy. Any fixed number is wrong on one of the two clusters, and the right number is
  a property of the cluster that day, not of this code.
- **Adapt the worker count across scrapes** (halve after a scrape that lost releases, grow after a
  clean one). Rejected: it cannot tell "the cluster is slow" from "there are more releases than the
  budget fits" — day 25's case, where the right answer is *more* concurrency, not less. It also
  carries state between scrapes for a decision that can be made inside one.
- **A per-call rate limit or token bucket.** Rejected: it caps the load but does not decide *which*
  releases get answered completely, which is the thing that matters when the budget is short.
- **Prioritise releases that are unreported or failing.** Tempting, and rejected for now: a release
  reads as failing only after it has been read, so the priority would come from the previous scrape
  — state between scrapes again, and a way to keep re-reading the same broken release while healthy
  ones go dark. Worth revisiting if the alert on unreported releases proves noisy in practice.
- **A cache, or a watch.** Still the answer if the release count keeps growing; still rejected here,
  for the reasons in ADR-0014.
