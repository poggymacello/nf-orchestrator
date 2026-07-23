# NF lifecycle states

The orchestrator models every deployment as one of four states:
`NOT_INSTANTIATED → INSTANTIATING → INSTANTIATED → FAILED`. As of M3 the reconciler emits these
states, deriving each from live Kubernetes and Helm signals at read time. This document reconciles
the model against what a real deploy, failure, and teardown actually produce.

## State diagram

```mermaid
stateDiagram-v2
    [*] --> NOT_INSTANTIATED
    NOT_INSTANTIATED --> INSTANTIATING: intent submitted
    INSTANTIATING --> INSTANTIATED: workload becomes healthy
    INSTANTIATING --> FAILED: workload cannot become healthy
    INSTANTIATED --> NOT_INSTANTIATED: teardown
    FAILED --> NOT_INSTANTIATED: teardown
```

## Transition table

Signals in the last column are what the reconciler read to derive each transition, observed on
2026-07-23 unless noted.

| Transition | Trigger | Underlying signal the reconciler derived from | Status |
|---|---|---|---|
| `NOT_INSTANTIATED` → `INSTANTIATING` | Intent submitted, deploy engine calls `helm upgrade --install` | Helm release status is `deployed` and pods are not all `Running` yet. Observed: `helm=deployed pods=[('Pending','ContainerCreating'),('Pending','ContainerCreating')]` at 08:11:43 | emitted by the reconciler (M3) |
| `INSTANTIATING` → `INSTANTIATED` | Every pod reaches `Running` | All pod phases are `Running`. Observed one second later: `helm=deployed pods=[('Running',None),('Running',None)]` at 08:11:44 | emitted by the reconciler (M3) |
| `INSTANTIATING` → `FAILED` | A pod cannot become healthy | Any pod waiting-reason in `ImagePullBackOff` / `ErrImagePull` / `CrashLoopBackOff`. Observed by upgrading to a nonexistent image tag: `helm=deployed pods=[('Pending','ErrImagePull'),('Running',None),('Running',None)]` at 08:14:16, settling to `ImagePullBackOff`. `helm status` stayed `deployed` the whole time, so the state came from the pod reason, not the release | emitted by the reconciler (M3) |
| `INSTANTIATED` / `FAILED` → `NOT_INSTANTIATED` | Teardown | `DELETE /deployments/{name}` runs `helm uninstall`; the next read finds no release. Observed: `{"state":"NOT_INSTANTIATED","uninstalled":true}`, then `helm list` shows the release gone and no pods remain | emitted by the reconciler (M3) |

## What exists today vs planned

As of M3 the orchestrator emits all four states itself. `GET /deployments/{name}` reads the live
Helm release status and pod phases and returns the derived state; `DELETE /deployments/{name}`
uninstalls the release and returns `NOT_INSTANTIATED`. The derivation is a pure function,
`derive_state`, described in [ADR-0005](adr/0005-derive-lifecycle-state-from-cluster-signals.md) and
the [M3 build log](../build-log/m3-reconciler.md). State is derived on read, not stored.

Still `(planned)`: metrics and any push/event stream for these transitions are M4. The reconciler
polls on request and cannot report a transition nobody polled for.

## Why these states exist

This four-state model is a simplified version of two things ETSI NFV and O-RAN already define
publicly, not an invented scheme.

ETSI NFV's VNF Lifecycle Management interface (SOL003) defines an `instantiationState` attribute
with exactly two values, `NOT_INSTANTIATED` and `INSTANTIATED`, plus a separate
`LcmOperationStateType` for in-flight operations: `STARTING`, `PROCESSING`, `COMPLETED`,
`FAILED_TEMP`, `FAILED`, `ROLLING_BACK`, `ROLLED_BACK`
([ETSI GS NFV-SOL 003 V3.5.1](https://www.etsi.org/deliver/etsi_gs/NFV-SOL/001_099/003/03.05.01_60/gs_NFV-SOL003v030501p.pdf)).
This project's `INSTANTIATING` collapses `STARTING`/`PROCESSING` into one state, and `FAILED`
maps directly onto the SOL003 `FAILED` operation state.

O-RAN's O2 interface (Working Group 6, Cloudification and Orchestration) manages NF Deployments
by aligning its lifecycle, fault, and performance services to these same ETSI NFV SOL APIs
([O-RAN Alliance specifications](https://www.o-ran.org/specifications)). A real SMO-driven
orchestrator reports deployment state this way so that anything watching it — a dashboard, an
alarm system, a rollback controller — has a single small enum to react to instead of parsing raw
Kubernetes pod phases or Helm release output directly.

## Reference: raw signals vs the four states

The four states are a projection over lower-level signals, not a replacement for them:

- **Kubernetes pod phase**: `Pending`, `ContainerCreating`, `Running`, `CrashLoopBackOff`,
  `ErrImagePull`, `ImagePullBackOff`, `Terminating` — observed directly via `kubectl get pods`.
- **Helm release status**: `deployed`, `failed`, `uninstalled` — observed via `helm status`. As
  seen above, `deployed` does not imply the workload is healthy; it only means the manifest was
  applied successfully.

The reconciler's job is to read the first two and report one of the four states — not to invent new
information, but to compress two APIs' worth of raw signals into one.
