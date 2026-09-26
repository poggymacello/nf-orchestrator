# ADR-0019: Alert rules are tested, against the series the drills produced

- **Status:** Accepted
- **Date:** 2026-09-26
- **Closes:** findings 2 and 3 of
  [drill 12](../../operations/postmortems/2026-09-26-drill-12-a-starved-host-and-a-frozen-cluster.md)
- **Refines:** [ADR-0007](0007-stability-is-an-alerting-concern.md), which put stability into
  `for:` windows, and the throttling rule from
  [ADR-0017](0017-reachability-needs-more-than-one-witness.md)

## Context

Since ADR-0007, some of the orchestrator's most important decisions live in PromQL, not Python: what
counts as stable, how long a failure must last, and what share of a window counts as throttled. The
Python has around two hundred unit tests. The PromQL had none. An alert was only checked when a drill ran Prometheus
alongside an incident, and then only for the series that incident produced.

Drill 12 found what that misses. Day 29's throttling rule averaged `nf_cluster_busy` over two
minutes. `avg_over_time` averages the samples that exist, and in an outage the busy series stops.
Six busy samples from the first thirty seconds of a real outage became the whole window, and the
rule went pending. It missed paging by about two seconds. Reading the expression didn't reveal this,
and no earlier drill had produced a series that stopped.

## Decision

**1. Alert rules are unit-tested with promtool, in CI.** `monitoring/tests/` holds promtool tests
whose input series are the drills' own series, written down from the postmortems. `lint-test` runs
`promtool check rules` and `promtool test rules`, using the same pinned Prometheus image the drills
used.

**2. A test whose series disappears is part of the suite.** Series that stop are the case every
range function handles differently from what its name suggests. Drill 12's outage is the first such
test.

**3. Copied expressions are pinned to the originals.** `promql_expr_test` needs its own copy of an
expression, and a copy goes stale without anyone noticing. A pytest fails if a copied expression no
longer matches a rule.

**4. A share of a window is divided by the scrapes attempted, not by the samples that survived.**
`count_over_time(up[...])` is the denominator, because `up` has a sample for every scrape, successful
or not.

## Consequences

- A rule change that would re-open a drilled failure now fails the build, rather than waiting for a
  drill to reproduce the same series.
- The test series are hand-transcribed from postmortems, so they are only as faithful as the
  transcription. Each test's comment names the drill it replays, and states the measured shape it
  follows.
- `promtool test rules` compares annotations exactly for any alert a test expects to fire. Positive
  cases therefore go through `promql_expr_test` on the expression, and `alert_rule_test` is used
  where no alert is expected. This keeps annotation wording out of the tests.
- CI needs Docker in `lint-test`. The runners have it; `secret-scan` already depends on it.

## Alternatives considered

**Keep verifying alerts only in drills.** That is how finding 2 got through. A drill tests the
series it produces. It can't cover a property that shows up in a series no drill has produced yet.

**Replace `avg_over_time` with `min_over_time(...) == 1`.** That would bring back day 29's problem:
at moderate load the busy signal flaps, and the alert would never fire.

**Record busy as a counter and rate it.** It could be made to work, but it would change the metric's
meaning in the runbooks. The ratio over `up` keeps the gauge and fixes the arithmetic.
