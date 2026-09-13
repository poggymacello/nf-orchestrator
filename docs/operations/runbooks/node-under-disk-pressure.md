# Runbook — node under disk pressure

**Use when:** a deployment reports `FAILED` and its pods show `reason: Evicted`, or `kubectl` shows the
node with `DiskPressure=True`.

**Severity:** Sev-2. The orchestrator reports this correctly since 2026-09-12 — `FAILED` during the
incident, `INSTANTIATED` once the intent is satisfied again.

Written from [drill 5](../postmortems/2026-09-12-drill-5-disk-pressure-and-eviction.md). Steps marked
**(not drilled)** were not run during that drill and are included because the runbook would be
incomplete without them; treat them with the corresponding caution.

## 1. Read what the orchestrator says

```bash
curl -s localhost:8000/deployments/<release>
```

During the drill:

```
{"state":"FAILED","desired_replicas":2,"ready_replicas":0,
 "pods":[{"phase":"Failed","reason":"Evicted","ready":false},   x8
         {"phase":"Pending","reason":null,"ready":false},       x2
         {"phase":"Succeeded","reason":null,"ready":false},     x2 ]}
```

`reason: "Evicted"` is what points here. More pods than `desired_replicas` is expected, not a second
problem: evicted pods are never deleted, and their replacements sit `Pending` behind a taint.

> **Before 2026-09-12** evicted pods reported `reason: null`, so the word that identifies this
> incident was missing from the response.

## 2. Confirm the node condition

```bash
kubectl --context <context> get node <node> -o jsonpath='{range .status.conditions[*]}{.type}={.status}{"\n"}{end}'
```

`DiskPressure=True` confirms it. While it holds, the node carries the taint
`node.kubernetes.io/disk-pressure:NoSchedule`, which is why replacement pods stay `Pending` — the
events say so directly:

```bash
kubectl --context <context> get events --sort-by=.lastTimestamp | grep -iE 'evict|disk|taint'
```

```
Warning  Evicted               pod/...   The node had condition: [DiskPressure].
Warning  FailedScheduling      pod/...   1 node(s) had untolerated taint(s)
Warning  EvictionThresholdMet  node/...  Attempting to reclaim ephemeral-storage
```

## 3. See how far past the threshold the node is

```bash
docker exec <node-container> df -BG /
```

Compare the `Avail` column with the node's `nodefs.available` eviction threshold, which is in the
kubelet configuration:

```bash
docker exec <node-container> grep -A4 evictionHard /var/lib/kubelet/config.yaml
```

On a default kind cluster that threshold is `0%` and this incident cannot happen at all — see
[drill 2](../postmortems/2026-08-20-drill-2-control-plane-unreachable.md#why-this-drill-replaced-the-disk-full-drill).

## 4. Find what is using the disk (not drilled)

The drill knew the cause, because it had created it. A real incident does not. Start from the
largest directories on the node's filesystem:

```bash
docker exec <node-container> sh -c 'du -xh / 2>/dev/null | sort -rh | head -20'
```

Container images and logs are the usual candidates on a kind node. This step was not exercised, and
deciding what is safe to delete from a node is a judgement the drill did not test.

## 5. Free the space

In the drill the cause was a known file:

```bash
docker exec <node-container> rm -f /var/drill-ballast
```

Nothing is to be done on the orchestrator's side. Do not redeploy — the release and its intent are
unchanged, and the ReplicaSet will reschedule once the taint lifts.

## 6. Watch the recovery, and wait for it to finish

```bash
for i in $(seq 1 30); do echo "$(date +%T) $(curl -s localhost:8000/deployments/<release> | grep -oE '"state":"[A-Z_]+"|"ready_replicas":[0-9]+' | tr '\n' ' ')"; sleep 6; done
```

Observed:

```
18:41:50  ballast removed
18:42:04  DiskPressure=False
18:42:25  ready=2
```

About 35 seconds from freeing the space to the intent being satisfied again. The state should now
read `INSTANTIATED`.

> **Before 2026-09-12 it stayed `FAILED` indefinitely**, because the evicted pods remain and the
> derivation judged the network function on all of them. If you are running an older build, a
> `FAILED` here after recovery is that bug, not an ongoing incident — check `ready_replicas` against
> `desired_replicas` instead.

## 7. Decide whether to clear the evicted pods (not drilled)

The recovered release still lists its evicted pods, each carrying `reason: "Evicted"`. That is kept
on purpose as the record of what happened, and it no longer affects the reported state. If the list
becomes a problem:

```bash
kubectl --context <context> delete pods -l app=<release> --field-selector=status.phase==Failed
```

The drill deleted its whole cluster instead, so this command was not run against a real release.

## What this runbook does not cover

- **Why the disk filled.** Freeing space ends the incident; it does not stop it recurring.
- **Memory or PID pressure.** Same kubelet mechanism, different signal, not drilled.
- **A multi-node cluster**, where evicted pods would reschedule onto other nodes instead of staying
  `Pending`. This project has one node.
