# Postmortem — drill 15: the orchestrator cannot spawn its tools

**Date:** 2026-10-16 · **Duration of the staged incident:** ~30 minutes · **Severity:** Sev-1 (staged: a false outage page)

**Summary:** The orchestrator was made unable to start helm, with the cluster healthy, in two ways.
In one, the binary was missing; in the other, the process table was full. Both were reported as
**the cluster being unreachable**, which is the page that sends someone to a control plane with
nothing wrong. The API answered bare `500`s that said nothing. With helm missing, `/readyz` answered
`200 ready` while every deploy and status call failed. Since day 12, any `OSError` at spawn had been
grouped with an outage. Both of these are the orchestrator's own host. They also need opposite
answers: a missing tool stays missing until someone installs it, and a full process table empties
on its own.

## What was staged

The orchestrator ran in a Linux container on the kind network, against a throwaway cluster
(`nf-drill15`, six releases). The image came from the drill image definition now committed at
[`manifests/drill.Dockerfile`](../manifests/drill.Dockerfile). It installs helm 4.2.2 and kubectl
1.35.0 from their projects' release sites, each checked against the published checksum. The first
build ran `main`'s code (before); the second ran the fix (after).

- **(a) helm missing.** The container started with `mv /usr/local/bin/helm /usr/local/bin/helm.missing`
  before uvicorn, so spawning helm raised `FileNotFoundError`. kubectl stayed in place.
- **(b) no spare pid.** A normal container, with `docker update --pids-limit` set to the number of
  pids already in use (6). Python's `fork` then failed with `BlockingIOError: [Errno 11] Resource
  temporarily unavailable`. With one pid spare, `fork` succeeded and the Go runtime crashed
  instead. That is drill 14's case, which the fix from day 33 already classifies correctly; it was
  checked, and it did.

The scratchpad that held drill 14's image recipe had been cleared between sessions. That is why the
recipe is now a committed file rather than something rebuilt from memory.

## Hypotheses, and what happened

Written before any run:

| | Predicted | Observed |
| --- | --- | --- |
| H1 | Both shapes: the scrape reports `nf_cluster_reachable 0` and `ClusterUnreachable` pages | **Held**, for both |
| H2 | Status routes answer a bare `500` | **Held**: `FileNotFoundError: 'helm'` and `BlockingIOError` in the server log, `Internal Server Error` to the caller, for status and deploy |
| H3 | `/readyz` says "too overloaded to tell" for a missing tool | **Wrong, and worse.** For (a) `/readyz` answered **`200 ready`**: it only ran kubectl, which was present. For (b) it answered `503` "did not start within 3s" about a fork that failed instantly |
| H4 | The two shapes need different answers: no retry for (a), retry for (b) | Became the design |

## Findings

**1. A healthy cluster was paged as unreachable, twice.** In both shapes the release listing raised
`OSError`, and the scrape's general handler (`except (DeployError, OSError)`) sets reachability to
0. **Fixed.**

**2. Bare 500s.** `run_bounded` did not catch `OSError`, so the exception escaped to the web
framework. Callers got `Internal Server Error` with no detail and no way to tell a broken install
from a broken cluster. **Fixed.**

**3. Ready while unable to work.** `/readyz` checked only that kubectl could reach the API server.
With helm gone, anything gating traffic on readiness, such as a load balancer or a Kubernetes
readiness probe, would have kept sending requests that all failed. **Fixed.**

**4. The readiness check misdescribed an instant failure.** "Did not start within 3s" was reported
about a `fork` that failed in about 2 ms. **Fixed.**

**5. Some spawn failures are still unclassified. Accepted.** Only `FileNotFoundError`,
`PermissionError`, `EAGAIN` and `ENOMEM` are classified. Any other `OSError` at spawn (`E2BIG`, for
one) is re-raised rather than guessed at. Those still reach the scrape's general handler. A unit
test pins the behaviour.

## The fix

[ADR-0022](../../design/adr/0022-a-failed-spawn-is-classified-by-its-cause.md).

- **A spawn failure is classified at the spawn.** A missing or non-executable tool is
  **`HostMisconfigured`**: HTTP `500` that names the tool, with no `Retry-After`, because waiting
  will not help. `EAGAIN` or `ENOMEM` is day 33's **`HostExhausted`**: `503` with `Retry-After`.
  The readiness probe's own kubectl is classified the same way.
- **`/readyz` requires both tools** on `PATH` before it probes. The lookup is a `PATH` search, not a
  process start.
- **The scrape makes no cluster claim when the host failed.** A missing tool counts as
  `reason="host"` and sets the new `nf_orchestrator_misconfigured` gauge.
- **`OrchestratorMisconfigured`** (`page`, `for: 1m`) fires on that gauge. Its runbook,
  [orchestrator misconfigured](../runbooks/orchestrator-misconfigured.md), is written from this
  drill's output. promtool tests cover it firing and not firing.
- **Unit tests are isolated from the test machine's `PATH`.** A conftest stand-in reports both tools
  as installed, and the one test about the lookup itself opts out with a marker.

## Verification

Same conditions on the fixed build. For (a), Prometheus ran with the branch's rules:

| | Before | After |
| --- | --- | --- |
| (a) scrape | `nf_cluster_reachable 0` | no claim about the cluster, `nf_orchestrator_misconfigured 1` |
| (a) alerts | `ClusterUnreachable` would page | **`OrchestratorMisconfigured` firing** from 17:47:37, `ClusterUnreachable` inactive |
| (a) status, deploy | `500 Internal Server Error` | `500` "helm is not installed or not executable on this host (No such file or directory): install it or fix PATH; retrying will not help", no `Retry-After` |
| (a) `/readyz` | **`200 ready`** | `500` "helm not found on PATH on this host" |
| (a) helm restored | — | reachable 1, misconfigured 0, `/readyz` 200, within one scrape, no restart |
| (b) scrape | `nf_cluster_reachable 0` | no claim about the cluster, `nf_orchestrator_exhausted 1` |
| (b) status | `500 Internal Server Error` | `503`, `Retry-After: 5`, "this host could not start helm: Resource temporarily unavailable (errno 11)…" |
| (b) `/readyz` | `503` "did not start within 3s" | `503` "this host could not start kubectl: … (errno 11)…" |
| (b) limit lifted | — | reachable 1, exhausted 0 |

Of the 11 new deploy-path tests, all fail against `main`'s code with `main`'s own conftest.

## What this drill did not cover

- A tool that is present but the wrong version (Helm 3, for example), which installs and lists but
  does not refuse field-ownership conflicts. Nothing detects it at runtime.
- Memory exhaustion as such. `ENOMEM` is classified, but only `EAGAIN` was produced.
- The Windows host. As in drill 14, the drill ran in a Linux container.
