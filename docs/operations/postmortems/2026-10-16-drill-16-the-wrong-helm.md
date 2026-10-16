# Postmortem — drill 16: the wrong major version of helm

**Date:** 2026-10-16 (day 35) · **Duration of the staged incident:** ~40 minutes · **Severity:** Sev-1 (staged: every deploy failing, with the cluster blamed)

**Summary:** The orchestrator ran with Helm 3.19.0 instead of Helm 4, against a healthy cluster.
Every deploy and repair failed, and the API answered each with **502, "the cluster answered and
refused"**. The cluster had never been asked: Helm 3 rejected the orchestrator's own arguments.
Meanwhile `/readyz` answered **200 ready**, `nf_orchestrator_misconfigured` read 0, and status reads
kept working, so the orchestrator looked healthy to everything that checks it.

Behind the loud failure was a quiet one, measured separately. Given arguments it accepts, Helm 3
**silently overwrote** a replica count that `kubectl scale` had set. That is the overwrite
[ADR-0008](../../design/adr/0008-forcing-field-ownership-is-an-explicit-operation.md) promises never
happens. The orchestrator's safety had rested on Helm 4 by convention only: the README and CI pinned
it, and nothing checked it at runtime.

## What was staged

A throwaway kind cluster (`nf-drill16`, v1.35.0) and two images from
[`manifests/drill.Dockerfile`](../manifests/drill.Dockerfile) with the same code, differing only in
`HELM_VERSION`: v4.2.2 (control) and v3.19.0. Both tools were checksum-verified at build time. Each
ran drill 3's scenario through the orchestrator's API on its own release:

1. deploy 2 replicas;
2. wait for `INSTANTIATED`;
3. `kubectl scale` the Deployment to 1, which makes kubectl another field manager;
4. resubmit the same intent.

## Hypotheses, and what happened

Written before any run:

| | Predicted | Observed |
| --- | --- | --- |
| H1 | Helm 3 silently overwrites kubectl's change on resubmit, and the API answers 201 | **Not reached through the API**, because H3 failed first. **Held when measured directly:** a Helm 3 upgrade after the scale reported `STATUS: deployed` and set replicas from 1 back to 2, with no conflict |
| H2 | `/readyz` says ready, and no metric differs | **Held:** `200 ready`, `nf_orchestrator_misconfigured 0` |
| H3 | Helm 3 parses the orchestrator's helm JSON the same way | **Wrong, and first.** Reads parsed fine. Every write failed: `Error: failed parsing --set-json data {"replicaCount": 2, "environment": "dev"}`. Helm 3's `--set-json` takes `key=value` pairs, not a whole object |
| H4 | `/repair` behaves the same under both | Moot: it fails the same way under Helm 3 |

The Helm 4 control behaved as designed throughout: deploy `201`, `INSTANTIATED`, and on resubmit a
server-side apply conflict with `kubectl.exe` answered **409**.

## Findings

**1. A wrong tool was reported as the cluster refusing.** The parse error matched no classifier, so
it fell to the default, a plain `DeployError`, HTTP 502. That is drill 14's lesson again: the
default answer for unrecognised text blames the component that is usually fine. **Fixed.**

**2. Nothing said the orchestrator could not do its job.** Readiness, metrics and reads were all
normal. Only a caller attempting a write would find out, and would be told the cluster was at fault.
**Fixed.**

**3. A latent silent overwrite.** Under Helm 3, an argument format Helm 3 accepts would have turned a
loud failure into ADR-0008's worst case: another manager's change overwritten with success reported.
The orchestrator's argument format blocked that by accident. The version gate now blocks it on
purpose. **Fixed.**

**4. kubectl's version is not checked. Accepted.** Its supported skew is relative to the API
server, so checking it would cost an API call, and nothing in this drill depended on it.

## The fix

[ADR-0023](../../design/adr/0023-the-tools-are-checked-not-assumed.md).

- **Helm's major version is checked:** `helm version --short` is parsed, and the answer cached for 60
  seconds. It is checked before every write, in `/readyz`, and in every scrape. A major other than 4
  is `HostMisconfigured`: HTTP 500, naming the version found and the version required, with no
  `Retry-After`. The scrape sets `nf_orchestrator_misconfigured 1`, which pages through
  `OrchestratorMisconfigured`.
- **Helm rejecting the orchestrator's arguments is classified as this host's problem,** never the
  cluster's. This covers a helm swapped while the cached answer is still valid.
- **What can't be read isn't called wrong.** An unparseable version string, or a helm that can't be
  run, is not reported as a wrong version.

## Verification

Same scenario, same images rebuilt from the fixed code:

| | Before (Helm 3) | After (Helm 3) | After (Helm 4, regression) |
| --- | --- | --- | --- |
| `/readyz` | **200 ready** | `500` "helm v3.19.0+g3d8990f is installed but Helm 4 is required (…): install Helm 4; retrying will not help" | `200` |
| Deploy | `502` "failed parsing --set-json data" | `500`, the same version message | `201` |
| Resubmit after drift | `502` | `500` | **`409`**, unchanged |
| `nf_orchestrator_misconfigured` | 0 | **1** | 0 |
| `nf_cluster_reachable` | 1 | 1 | 1 |

All 8 new deploy-path tests fail against `main`'s code.

## What this drill did not cover

- A Helm 4 minor or patch version that changes behaviour. Only the major is checked.
- kubectl version skew (finding 4).
- A helm upgraded in place while the orchestrator runs, within the 60-second cache window.
  Classified correctly by the error classifier, but not staged.
