# Postmortem — Drill 5: disk pressure and eviction

**Date:** 2026-09-12 · **Severity:** Sev-1 · **Status:** findings 1 and 2 fixed, finding 3 accepted ·
**Closes:** [drill 2](2026-08-20-drill-2-control-plane-unreachable.md)'s action item 4, open since
2026-08-20

**Summary:** The disk drill abandoned on day 11 finally ran. Under real kubelet eviction the
orchestrator got the state right and threw away the reason — twelve pods reported as
`{"phase": "Failed", "reason": null}` while Kubernetes knew every one had been evicted for
`DiskPressure`. Worse, when the pressure cleared and the workload came back to 2/2, the orchestrator
kept reporting **`FAILED` indefinitely**, because Kubernetes never deletes an evicted pod and the
derivation was judging the network function on corpses.

## Why this drill could finally run

Day 11 abandoned it for two reasons, and only one of them was fixable by configuration.

kind ships `evictionHard` at `0%` for every signal, which switches disk eviction off. That is a
config problem: a `kubeadmConfigPatches` block turns it back on.

The second reason stood: the node filesystem is the host's, with 843 GiB free, so crossing a
conventional `10%`-available threshold would mean writing something like 760 GiB to a laptop. That
is a hazard, not a drill.

So **the threshold was calibrated to the disk instead of the disk to the threshold.**
`nodefs.available: "840Gi"` against 843 GiB free leaves about 3 GiB of headroom, and a bounded
5 GiB write crosses it. Above the line kubelet behaves identically either way — it taints the node,
evicts pods, and refuses to schedule replacements. Only the arithmetic to get there is different,
and it is reversible with `rm`.

Run on a throwaway cluster, `nf-drill`, so the persistent `nf-orchestrator` one that every milestone
has reused was never touched. That needed `KUBE_CONTEXT` to stop being a hardcoded constant; it now
reads `NF_KUBE_CONTEXT` with the old value as the default.

## Timeline

```
18:35:30  deploy disk-nf, 2 replicas          -> INSTANTIATED, 2/2 ready
18:38:13  843G available, DiskPressure=False
18:38:14  fallocate 5G /var/drill-ballast     -> 836G available
18:38:27  DiskPressure=True
          orchestrator: FAILED, ready=0, 12 pods for a 2-replica intent
18:41:50  rm /var/drill-ballast               -> 842G available
18:42:04  DiskPressure=False
18:42:25  cluster: 2/2 Running again
          orchestrator: FAILED                <-- and still FAILED at 18:45:04
```

What the twelve pods were:

```
8  Failed     Evicted
2  Succeeded             (nginx exits 0 on SIGTERM — the drill-1 shape)
2  Pending               (FailedScheduling: untolerated taint node.kubernetes.io/disk-pressure)
```

The count stabilised at twelve rather than growing without bound, because the node gets tainted and
replacements go `Pending` instead of being created and evicted in a loop. I had expected a runaway
and was wrong.

## Finding 1 — the state was right and the reason was discarded

```
$ curl -s localhost:8000/deployments/disk-nf
{"state":"FAILED", "desired_replicas":2, "ready_replicas":0,
 "pods":[{"phase":"Failed","reason":null,"ready":false},   x8
         {"phase":"Pending","reason":null,"ready":false},  x2
         {"phase":"Succeeded","reason":null,"ready":false} x2 ]}

$ kubectl get pod ... -o jsonpath='{.status.reason} / {.status.message}'
Evicted / Pod was rejected: The node had condition: [DiskPressure].
```

`FAILED` is correct. But an operator reading the API response sees `reason: null` twelve times and
has to go to `kubectl` to learn the single word that explains the incident.

**Root cause.** `pod_states` reads the reason from `containerStatuses[*].state.waiting.reason`. An
evicted pod has no waiting container — it was rejected before one started — and its reason sits on
the pod itself, at `status.reason`. The reconciler looked in exactly one place for a signal that
lives in two.

Fixed: the pod-level reason is used as a fallback when no container is waiting. The same twelve pods
now report `reason: "Evicted"`.

## Finding 2 — a recovered network function reported FAILED forever

This is the serious one. At 18:42:25 the cluster was back to two Running pods and `DiskPressure` was
gone. The orchestrator reported `FAILED` at 18:42:25, at 18:43, at 18:44, at 18:45, and would have
gone on reporting it until somebody deleted the evicted pods by hand.

**Root cause.** Kubernetes does not delete evicted pods; they remain as `phase: Failed` records with
no `deletionTimestamp`. `pod_states` therefore included all eight, and
[ADR-0009](../../design/adr/0009-the-operation-is-not-the-network-function.md)'s rule — *any
non-terminating pod in a terminal phase means the workload stopped and was not replaced* — fired on
them permanently.

That rule was itself a fix, added on day 15 for [drill 1](2026-08-20-drill-1-pods-killed-mid-deploy.md)'s
finding 2, where `Succeeded` fell through to `INSTANTIATING` forever. It was correct for the case it
was written for and wrong for this one, and nothing in between had exercised the difference.

The distinction it was missing: **a terminal pod is history, not current state.** It matters only
when the intent is *not* otherwise satisfied.

`derive_state` now partitions the pods:

| | |
|---|---|
| a live pod carrying a failure reason | `FAILED` |
| `desired` live pods, all ready | `INSTANTIATED`, whatever corpses remain |
| a terminal pod with the intent unsatisfied | `FAILED` — drill 1's case, preserved |

Checked against every prior drill's shape before shipping: six running pods for a three-replica
intent still reports `INSTANTIATING`; three `Succeeded` pods with nothing replacing them still
reports `FAILED`; an `ImagePullBackOff` on a live pod still reports `FAILED`.

After, on the same cluster with the same eight corpses:

```
cluster:        2 Running, 8 Evicted, 2 Completed
orchestrator:   state=INSTANTIATED  desired=2  ready=2
                8 x phase=Failed reason=Evicted      <- still visible, no longer fatal
```

## Finding 3 — the pods array carries the whole incident, and is accepted

The response still lists all twelve pods, and would list a hundred after a longer incident, until
Kubernetes garbage-collects them. That is deliberate: the corpses are the evidence of what happened,
they carry `reason: Evicted` now, and hiding them would trade a small response for a silent one.
Accepted rather than fixed, and worth revisiting if a release ever accumulates enough of them to
make the response unwieldy.

## What went well

- `FAILED` during the incident was correct, promptly, from workload signals alone — `helm_status`
  stayed `deployed` throughout, as it has in every drill since M0.
- `last_operation` correctly stayed `COMPLETED`: the install genuinely did succeed, and the eviction
  was not an operation.
- Making `KUBE_CONTEXT` configurable meant the whole drill ran on a throwaway cluster; the
  persistent one was never at risk.
- The drill was safe: 5 GiB written and removed, against 843 GiB free.

## Action items

| # | Finding | Action | Status |
|---|---|---|---|
| 1 | An evicted pod's reason was discarded | Fall back to the pod-level `status.reason` when no container is waiting | **fixed 2026-09-12** |
| 2 | A recovered NF reported FAILED forever | Judge the NF on live pods; a terminal pod is a failure only when the intent is unsatisfied | **fixed 2026-09-12** |
| 3 | The pods array carries the whole eviction history | None. The corpses are the evidence, and they now carry their reason | **accepted** |

## Reproduce

```bash
kind create cluster --name nf-drill --config kind-drill-config.yaml
```

```bash
NF_KUBE_CONTEXT=kind-nf-drill uvicorn orchestrator:app --port 8000
```

```bash
docker exec nf-drill-control-plane fallocate -l 5G /var/drill-ballast
```

Watch `kubectl --context kind-nf-drill get node nf-drill-control-plane -o jsonpath='{.status.conditions[?(@.type=="DiskPressure")].status}'`
and `GET /deployments/disk-nf`. Recover with `docker exec nf-drill-control-plane rm -f /var/drill-ballast`,
then `kind delete cluster --name nf-drill`.

The threshold in `kind-drill-config.yaml` is calibrated against a host with ~843 GiB free. On a
different machine, set `nodefs.available` a few GiB below whatever `df` reports.
