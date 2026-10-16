# ADR-0022: A failed spawn is classified by its cause

- **Status:** Accepted
- **Date:** 2026-10-16
- **Closes:** findings 1 to 4 of
  [drill 15](../../operations/postmortems/2026-10-16-drill-15-the-orchestrator-cannot-spawn-its-tools.md)
- **Refines:** [ADR-0021](0021-a-host-out-of-capacity-is-not-a-slow-host.md), which classified a
  tool that started and then crashed for want of threads
- **Refined by:** [ADR-0023](0023-the-tools-are-checked-not-assumed.md), which also checks helm's
  major version, closing the "wrong version is still invisible" consequence below

## Context

Every cluster call starts helm or kubectl. If the start itself fails, Python raises `OSError` before
any child process exists. On day 12 that was grouped with an unreachable cluster, so that a scrape
would survive a missing binary "the same way it survives an outage".

Drill 15 tested that choice with the cluster healthy. A missing helm and a full process table both
produced the outage page, bare HTTP 500s from the API, and, with helm missing, a `/readyz` of
`200 ready`. The day-12 grouping was right that the scrape must survive. It was wrong about what to
report: neither failure says anything about the cluster, and the two need opposite responses.

## Decision

**1. A spawn failure is classified where it happens, by its cause.**

| Cause | Class | HTTP | Retry |
|---|---|---|---|
| `FileNotFoundError`, `PermissionError` | `HostMisconfigured` | `500` | No `Retry-After`: nothing changes until someone installs the tool |
| `errno` `EAGAIN` or `ENOMEM` | `HostExhausted` (ADR-0021) | `503` | `Retry-After`: the process table empties on its own |
| Any other `OSError` | re-raised | — | Not guessed at |

**2. Readiness means able to do the job.** `/readyz` requires both helm and kubectl on `PATH`, found
by a `PATH` lookup, before it probes the API server.

**3. A misconfigured host has its own series and page.** `nf_orchestrator_misconfigured` and
`OrchestratorMisconfigured` (`page`, one minute). The scrape counts the affected releases as
`reason="host"` and makes no claim about the cluster.

## Consequences

- A broken install pages as a broken install. Before, it paged as a cluster outage.
- `500` is used for exactly one orchestrator-side condition, and its detail names the tool and says
  retrying will not help. Every other `5xx` is either the cluster's answer (`502`, `503` without
  `Retry-After`) or a transient condition with `Retry-After`.
- A wrong version of a tool is still invisible. The tool is present, starts, and answers.
- The unit tests no longer depend on helm and kubectl being on the test machine's `PATH`.

## Alternatives considered

**Check for the tools once at start-up and refuse to start.** That fails fast for a broken image, but
not for a tool removed or a `PATH` changed while the process runs. It also turns a misconfiguration
into a crash loop, which is noisier and says less than a `500` that names the tool.

**Treat every `OSError` at spawn as `HostExhausted`.** That is simpler, but it tells callers to
retry a missing binary forever.

**Keep grouping spawn failures with outages, and document it.** Drill 15 showed what that costs: the
on-call engineer is sent to the one component that is fine.
