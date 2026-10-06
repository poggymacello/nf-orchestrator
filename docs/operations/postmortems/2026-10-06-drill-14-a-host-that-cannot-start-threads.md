# Postmortem — drill 14: a host that cannot start threads

**Date:** 2026-10-06, re-verified 2026-10-07 · **Duration of the staged incident:** ~1 hour across both days · **Severity:** Sev-1 (staged: a false outage page)

**Summary:** The orchestrator ran in a Linux container with a pids limit, the way it would run in a
real deployment that sets one, and the cluster stayed healthy. Linux counts threads against
`pids.max`. helm and kubectl are Go programs, and each starts several threads. So the orchestrator's
own calls crashed before they reached the cluster, and its classifier read the crash as the
cluster's reply. At the milder limit, eight of ten network functions lost their monitoring with
nothing recorded about why. At the tighter one, it **paged that the cluster was unreachable**. The
API meanwhile answered 502 "the cluster answered and refused" with 19 KB of Go stack trace, and five
of six concurrent requests got bare 500s from the web framework's own thread pool.

## What was staged

The first plan was a Windows Job Object with an active-process limit on the orchestrator's process
tree. It did not bind. Four of four child processes started under a limit of **1**. The venv's
Python launcher runs the interpreter inside its own job with silent breakaway allowed, so the
interpreter's children leave every job, including the one meant to limit them. I discarded that
rather than present a test that wasn't working, and the drill moved to the faithful setting.

- **Image:** throwaway, built for the drill from `debian:trixie-slim`. It contained the
  orchestrator from `main`, helm v4.2.2 from helm's release site, and kubectl 1.35 copied out of
  the kind node image.
- **Network:** the `kind` Docker network, so the container reached a throwaway cluster
  (`nf-drill14`, ten releases) by its internal name.
- **The limit:** `docker update --pids-limit N` on the running container.

Baseline with no limit: 7 pids at rest, and a peak of **57** during a scrape (96 on the fixed
build's verification run). The limit was then set to 30, and then to 15. The cluster was never
touched.

Docker Desktop restarted between the two days. The original container exited with 255 when it did,
with ordinary log lines up to the end, and was restarted for the "before" alert observation. That
restart is not a finding.

## Hypotheses, and what happened

The hypotheses were written before any run. H6 was added before any run too, when the drill moved
to Linux.

| | Predicted | Observed |
| --- | --- | --- |
| H1 | A failed spawn (`OSError`) makes the scrape report the cluster unreachable | **The outcome held; the mechanism was wrong.** Forking a Go binary needs one pid and succeeded. The crash came afterwards, inside the Go runtime: `runtime: failed to create new OS thread (have 10 already; errno=11) … fatal error: newosproc`, exit 2. That text was classified as a plain `DeployError`, and a listing failure of any `DeployError` sets `nf_cluster_reachable 0` |
| H2 | A bare 500 from the status route | Held, from Starlette's thread pool, not from the orchestrator's own code |
| H3 | The host alert stays silent | **Held:** the host's share was 0.03–0.05s, because processes started fast and crashed |
| H4 | `/readyz` claims a timeout for a spawn that failed instantly | Moot: `/readyz` returned 502 with the Go stack |
| H5 | Failures follow concurrency, not particular releases | **Held:** 8 of 10 on every scrape at limit 30, while single calls succeeded |
| H6 | Thread creation itself fails (`RuntimeError: can't start new thread`) | **Held:** a test script inside the container couldn't start eight threads, and five of six concurrent requests died on it |

## Findings

**1. A healthy cluster was paged as unreachable.** At `--pids-limit 15`, every scrape reported
`nf_cluster_reachable 0`. With Prometheus running, `ClusterUnreachable` fired from 17:52:53 while
`OrchestratorHostOverloaded` stayed inactive. **Fixed.**

**2. The API blamed the cluster, at length.** Status and readiness calls answered **502**, which
this API documents as "the cluster answered and refused this operation". The detail carried the
whole Go runtime stack: **18,933 bytes** for one status call. **Fixed.**

**3. Concurrent requests got bare 500s.** FastAPI runs synchronous routes on a thread pool, and
starting a pool thread raised `RuntimeError: can't start new thread` before any orchestrator code
ran. Five of six concurrent status calls got `Internal Server Error`, with no classification and no
`Retry-After`. **Fixed.**

**4. Lost coverage was filed under "error", with no cause.** At `--pids-limit 30`, 8 of 10 releases
were `nf_releases_unreported{reason="error"}` on every scrape. The scrape-over-budget runbook's
branch for that reason says *reading that release failed — read it directly*, and reading it
directly worked (200), because one call alone fits under the limit. The exception text was
discarded, so the orchestrator had no record of why. **Fixed.**

**5. The host alert could not see a host that crashes fast.** `OrchestratorHostOverloaded` watched
how long the host took to start kubectl. A host at its pids limit starts processes quickly and then
loses them, so its share stayed near zero. **Fixed.**

**6. The orchestrator does not size its own pids needs. Accepted.** A ten-release scrape at the
default of 8 workers peaked at 57–96 pids: about ten Go threads per helm or kubectl process, plus
the orchestrator's own. Whatever runs it has to allow for that. The runbook now gives the measured
numbers; the orchestrator doesn't police its own limit.

## The fix

[ADR-0021](../../design/adr/0021-a-host-out-of-capacity-is-not-a-slow-host.md).

- **`HostExhausted`**, a kind of `HostOverloaded`, covers the two strings the Go runtime printed in
  the drill. It is a 503 with `Retry-After`. The detail keeps the runtime's own first two lines and
  drops the stack: 203 bytes instead of 18,933. The readiness check classifies its own kubectl's
  crash the same way.
- **The scrape records exhaustion rather than dying of it.**
  - A release that crashes this way counts as `reason="host"`.
  - A worker thread that can't be started becomes a recorded result, not an exception that would
    end the scrape.
  - A probe thread that can't be started means there is no probe.
  - A listing that crashes this way makes no claim about the cluster.
  - The new `nf_orchestrator_exhausted` gauge is 1 for any scrape that hit the limit anywhere.
- **The app answers Python's `can't start new thread` with the same 503.** Any other
  `RuntimeError` still surfaces as a 500.
- **`OrchestratorHostOverloaded` gains a second half:** a quarter of scrape attempts exhausted over
  two minutes, counted over `up` in the ADR-0019 form. promtool replays drill 14's series: the old
  expression returns nothing, and the new one fires.

## Verification, 2026-10-07

The same container setup with the fixed build, Prometheus running against the branch's rules:

| | Before | After |
| --- | --- | --- |
| Scrape, limit 15 | `nf_cluster_reachable 0` | no claim about the cluster, `nf_orchestrator_exhausted 1` |
| `ClusterUnreachable` | **firing** from 17:52:53 | inactive |
| `OrchestratorHostOverloaded` | inactive | **firing** from 17:55:08 |
| Status route, limit 15 | 502, 18,933 bytes | 503 `Retry-After: 5`, 203 bytes, names the host |
| `/readyz`, limit 15 | 502 with Go stack | 503, names the host |
| Six concurrent calls | 5×500, 1×502 | 6×503 |
| Scrape, limit 30 | 8 releases `reason="error"`, nothing else | 8 releases `reason="host"`, reachable 1, busy 0, exhausted 1 |
| Limit removed | — | exhausted 0, every release reported, within seconds |

## What this drill did not cover

- A spawn that fails in Python itself (`fork` returning `EAGAIN`, so `OSError` before any child
  exists). The Go runtime ran out first at both limits tried. That path still reaches the scrape's
  general handler. **(not drilled)**
- A missing binary (`FileNotFoundError`), which day 12 chose to treat like an outage. It is a host
  misconfiguration, not a cluster fault, and is the same kind of mislabel as this drill's, but it
  wasn't staged.
- Memory exhaustion, which on Linux reaches the same thread-creation failure by a different route.
- The Windows host. Its process-limit mechanism couldn't be applied to this Python, as described
  above.
