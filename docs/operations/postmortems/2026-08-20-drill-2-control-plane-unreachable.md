# Postmortem — Drill 2: control plane unreachable

**Date:** 2026-08-20 · **Severity:** Sev-1 · **Outage window:** 20:50:02 → 20:50:31 (29s) ·
**Status:** findings 1-3 fixed 2026-08-21, finding 4 closed 2026-09-12

**Summary:** Stopped the kind control-plane container with a healthy deployment running, then
exercised the API. The deploy path behaved correctly: `502` with the real error, and the failure
counter incremented. The read path did not. `GET /deployments/drill-one` returned **HTTP 200 with
`state: NOT_INSTANTIATED`** — a running NF reported as never deployed, in a response byte-identical
to the one returned after a real teardown. `/healthz` reported `ok` throughout.

## Environment

Same as [drill 1](2026-08-20-drill-1-pods-killed-mid-deploy.md): single-node kind cluster
`nf-orchestrator`, Helm v4.2.2, kubectl v1.36.1, Kubernetes v1.36.1, orchestrator on
`127.0.0.1:8000`. Release `drill-one` was deployed and `INSTANTIATED` when the drill started.

## Why this drill replaced the disk-full drill

Day 10 planned drill 2 as "node disk full", to watch a deployment fail under `DiskPressure`. That
drill was abandoned before running it, for two reasons found while setting it up.

kind switches kubelet's disk eviction off:

```
$ docker exec nf-orchestrator-control-plane grep -iE "eviction|imagefs|nodefs" \
    /var/lib/kubelet/config.yaml
evictionHard:
  imagefs.available: 0%
  nodefs.available: 0%
  nodefs.inodesFree: 0%
evictionPressureTransitionPeriod: 0s
```

With every hard threshold at `0%`, no amount of filling produces `DiskPressure` until the
filesystem is literally at zero bytes. The node agrees:

```
$ kubectl get node nf-orchestrator-control-plane -o jsonpath='{...conditions}'
DiskPressure=False (KubeletHasNoDiskPressure)
```

And the node filesystem is the host's, not a bounded volume:

```
$ docker exec nf-orchestrator-control-plane df -h /
Filesystem      Size  Used Avail Use% Mounted on
overlay        1007G  108G  848G  12% /
```

Filling it would mean writing ~760 GB to the developer machine's own disk, shared with the Docker
Desktop WSL2 VM, in order to cross a threshold that is disabled. That is a large real risk for no
signal. A meaningful disk drill needs either an eviction threshold configured on the kind node or a
size-bounded volume; both are changes to the cluster, not drills against it. Recorded as a
prerequisite rather than run badly.

Stopping the control plane was substituted because it produces a real failure, is recoverable in
seconds, and exercises the same question: what does the orchestrator report when it cannot see the
truth?

## Hypothesis before the drill

Deploys should fail loudly with a 5xx. Reads should also fail loudly — an unreachable cluster is
not information about a deployment, so `GET /deployments/{name}` should error rather than answer.

## Timeline

```
20:50:02  docker stop nf-orchestrator-control-plane
          $ kubectl get nodes
          Unable to connect to the server: dial tcp 127.0.0.1:64114:
            connectex: No connection could be made because the target machine actively refused it.

20:50:13  POST /deployments {"name":"drill-two","replicas":2,"environment":"staging"}
          -> 502 in under 1s
          {"detail":"Error: kubernetes cluster unreachable: Get \"https://127.0.0.1:64114/version\":
            dial tcp 127.0.0.1:64114: connectex: No connection could be made because the target
            machine actively refused it."}

20:50:13  GET /deployments/drill-one
          -> 200 in under 1s
          {"release":"drill-one","state":"NOT_INSTANTIATED","helm_status":null,"pods":[]}   <-- finding 1

20:50:14  GET /healthz   -> 200 {"status":"ok"}                                             <-- finding 3
20:50:14  GET /metrics
          deployments_total{environment="dev",result="success"} 1.0
          deployments_total{environment="staging",result="failed"} 1.0

20:50:26  docker start nf-orchestrator-control-plane
20:50:31  API server answering again (2 kubectl attempts, ~5s after start)

20:50:31  GET /deployments/drill-one -> INSTANTIATED  pods=[Running, reason=null]
20:50:32  GET /deployments/drill-one -> INSTANTIATED  pods=[Running, reason=CreateContainerConfigError]  <-- finding 2
20:50:41  GET /deployments/drill-one -> INSTANTIATED  pods=[Running, reason=null]  (settled)
```

## Finding 1 — an unreachable cluster is reported as `NOT_INSTANTIATED`, with HTTP 200

This is the response during a total control-plane outage, for a release that was deployed and
running:

```
{"release":"drill-one","state":"NOT_INSTANTIATED","helm_status":null,"pods":[]}
```

And this is the response after tearing the same release down for real, at the end of the drill:

```
{"release":"drill-one","state":"NOT_INSTANTIATED","helm_status":null,"pods":[]}
```

They are identical. Nothing in the payload or the status code distinguishes "this NF does not
exist" from "I cannot reach the cluster to find out".

**Root cause.** `helm_release_status()` wraps `helm status` in
`try: ... except DeployError: return None`. That `except` was written for the one case where Helm
exits non-zero because the release is absent, but it catches every non-zero exit, including
`kubernetes cluster unreachable`. `derive_state` then maps `helm_status is None` to
`NOT_INSTANTIATED`, and `reconcile` never sees that anything went wrong. The absent-release case
and the cannot-reach case were collapsed into one sentinel value.

The consequence is worse than an outage. A dashboard driven by this endpoint would show every
deployment in the estate transitioning to `NOT_INSTANTIATED` at the moment the control plane went
down — a fleet-wide teardown that never happened. Anything automated watching for
`NOT_INSTANTIATED` would act on it.

Note the asymmetry: the deploy path got this right for free, because `DeployError` propagates out
of `create_deployment` into a 502 with the underlying message. The read path is the one that
swallows it.

## Finding 2 — `CreateContainerConfigError` reads as `INSTANTIATED`

During recovery, the pod's container could not be created at all, and the reconciler called it
`INSTANTIATED`:

```
{"release":"drill-one","state":"INSTANTIATED","helm_status":"deployed",
 "pods":[{"phase":"Running","reason":"CreateContainerConfigError"}]}
```

The events show why the container failed, and that it was transient — kubelet had not yet synced
services after the restart, so it could not build the container's environment variables:

```
16s  Warning  Failed          pod/drill-one-...  Error: services have not yet been read at least
                                                 once, cannot construct envvars
15s  Warning  BackOff         pod/drill-one-...  Back-off restarting failed container nf
10s  Normal   Started         pod/drill-one-...  Container started
```

The pod recovered on its own about ten seconds later. The reporting did not: pod `phase` was
`Running` while the container inside it was in `waiting`, and `CreateContainerConfigError` is not
one of the three strings in `FAILED_POD_REASONS`. This is action item 3 from drill 1 confirmed
from a second direction — a failure allowlist is open-ended by construction, and pod phase is not
container health.

## Finding 3 — `/healthz` reports `ok` with every dependency down

`GET /healthz` returned `{"status":"ok"}` while the cluster was unreachable and every deploy was
failing. It is a liveness check on the Python process and nothing more. That is a defensible
definition, but it is not written down anywhere, so a reader — or a probe — may reasonably read it
as "the orchestrator is working".

## What went well

- The deploy path failed correctly and fast: `502`, under a second, with the real Helm error
  passed through intact rather than flattened to "internal error".
- `deployments_total{environment="staging",result="failed"}` incremented on the failed attempt, so
  the M4 counter recorded a failure it had never been tested against.
- Recovery needed no intervention: `docker start`, ~5s to a responding API server, ~10s more for
  the pod to settle. The kind cluster survived a stop/start with the release intact.

## Action items

| # | Finding | Action | Status |
|---|---|---|---|
| 1 | Unreachable cluster reported as `NOT_INSTANTIATED` / 200 | Distinguish "release not found" from "cluster unreachable" in `helm_release_status`; the second must surface as an error state or a 5xx, never as a lifecycle state | **fixed 2026-08-21** — `ClusterUnreachable` propagates, every route returns 503 ([ADR-0006](../../design/adr/0006-instantiated-means-the-intent-is-satisfied.md)) |
| 2 | `CreateContainerConfigError` reads as `INSTANTIATED` | Same fix as drill 1 action item 3: use container readiness, not a reason allowlist | **fixed 2026-08-21** — covered by unit test; the live recovery window did not reproduce on the 08-21 replay |
| 3 | `/healthz` is green with all dependencies down | Either document it as process-liveness only, or add a readiness endpoint that checks cluster reachability | **fixed 2026-08-21** — both: `/healthz` documented and pinned as liveness, new `/readyz` returns 503 when unreachable |
| 4 | Disk drill has no failure to observe on kind | Configure an eviction threshold on the kind node, or bound the workload's disk with a volume, before attempting drill 3 | **closed 2026-09-12** — `kind-drill-config.yaml` turns eviction back on with the threshold calibrated to the host's free space, and [drill 5](2026-09-12-drill-5-disk-pressure-and-eviction.md) ran on it |

## Reproduce

```bash
docker stop nf-orchestrator-control-plane
curl -s -X POST localhost:8000/deployments -H 'Content-Type: application/json' \
  -d '{"name":"drill-two","replicas":2,"environment":"staging"}'
curl -s localhost:8000/deployments/drill-one
curl -s localhost:8000/healthz
docker start nf-orchestrator-control-plane
```

## Follow-up, 2026-08-21

Findings 1-3 fixed on Day 12 and re-verified by stopping the control plane again with the same
release running:

```
GET    /deployments/drill-one -> 503 {"detail":"Error: kubernetes cluster unreachable: ..."}
POST   /deployments           -> 503
DELETE /deployments/drill-one -> 503
GET    /healthz               -> 200 {"status":"ok"}
GET    /readyz                -> 503 {"detail":"Unable to connect to the server: ..."}
```

`DELETE` is the one worth calling out. Before the fix it would have gone through
`helm_release_status` → `None` → "no release here" and returned
`{"uninstalled": false}` with HTTP 200 — reporting a successful idempotent teardown of a release
that was still running. That path was never exercised during the original drill.
