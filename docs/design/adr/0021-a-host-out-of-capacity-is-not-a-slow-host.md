# ADR-0021: A host out of capacity is its own failure, not a slow host or a refusing cluster

- **Status:** Accepted
- **Date:** 2026-10-06
- **Closes:** findings 1 to 5 of
  [drill 14](../../operations/postmortems/2026-10-06-drill-14-a-host-that-cannot-start-threads.md)
- **Refines:** [ADR-0018](0018-the-probe-measures-two-ends.md), which introduced `HostOverloaded`
  for a host that is slow

## Context

Every cluster call the orchestrator makes is a child process, and every route runs on a thread.
ADR-0018 gave the orchestrator a way to say "this host is slow". That took measuring how long kubectl
took to start, and it assumed the process would start.

Drill 14 ran the orchestrator in a Linux container with a pids limit, where threads count as well as
processes. helm and kubectl are Go programs. Forking one takes a single pid, then its runtime asks
for more threads and dies when it can't: `runtime: failed to create new OS thread ... fatal error:
newosproc`, exit 2. That is not slowness; the host's measured share stayed near zero. Nothing
recognised the text, so it fell to the default classification: the cluster answered and refused.
That meant HTTP 502, `reason="error"` in the scrape, and, when the release listing was the call that
crashed, `nf_cluster_reachable 0`. The web framework's own thread pool failed the same way and
produced bare 500s.

## Decision

**1. A crash for want of threads is `HostExhausted`, a kind of `HostOverloaded`.** It is matched on
the two strings the Go runtime printed in the drill. It means a 503 with `Retry-After`, a detail
that keeps the runtime's first two lines and drops the stack, `reason="host"` in the scrape, and no
claim about the cluster.

**2. Running out of threads inside the orchestrator is the same incident.** A scrape worker or probe
thread that can't be started is recorded as exhaustion, not raised. The app answers Python's
`can't start new thread` with the same 503. Every other `RuntimeError` keeps its 500.

**3. Exhaustion has its own series and pages through the host alert.** The
`nf_orchestrator_exhausted` gauge is 1 for any scrape that hit the limit anywhere.
`OrchestratorHostOverloaded` now also fires when a quarter of the scrape attempts in two minutes
were exhausted, taken as a share of `up` per ADR-0019.

## Consequences

- A healthy cluster behind an exhausted orchestrator no longer pages as an outage, and the page
  that does fire names the right machine.
- Classification again depends on another program's wording, as in ADR-0018. The strings come from
  helm v4.2.2 and are pinned in a unit test. If Go changes them, the failure falls back to a plain
  `DeployError`. That is the old, misleading answer, so a Go upgrade warrants re-running this drill.
- "Can't start a thread" and "slow to start a process" share an alert but not a series. The runbook
  separates them by which half of the rule fired.
- The orchestrator still doesn't size or police its own pids use. The runbook gives the measured
  peak (57–96 pids for a ten-release scrape at 8 workers).

## Alternatives considered

**Classify by exit code 2.** Go uses exit 2 for every fatal runtime error and for ordinary usage
errors, so it isn't specific to this.

**Lower the scrape's concurrency when exhaustion is seen.** That is plausible as a later step, but
it changes behaviour under a fault the orchestrator has only just learned to name. Naming it comes
first.

**Treat every `OSError` and `RuntimeError` as host exhaustion.** It would cover the cases not
drilled (a failed `fork`, a missing binary) along with genuine bugs that happen to raise the same
types. Matching what was observed keeps a programming error from being reported as a capacity
problem.
