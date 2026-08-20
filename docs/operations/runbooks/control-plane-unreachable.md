# Runbook — control plane unreachable

**Use when:** deploys return `502 kubernetes cluster unreachable`, or every deployment suddenly
reads `NOT_INSTANTIATED` at once.

**Severity:** Sev-2 while the orchestrator is merely unavailable. Sev-1 the moment it starts
reporting states as if they were true — see the warning below.

Every command here was run during
[drill 2](../postmortems/2026-08-20-drill-2-control-plane-unreachable.md) on 2026-08-20.

## Warning: `NOT_INSTANTIATED` may be a lie

`GET /deployments/{name}` returns HTTP 200 with this body when the cluster is unreachable:

```
{"release":"drill-one","state":"NOT_INSTANTIATED","helm_status":null,"pods":[]}
```

It returns exactly the same body when the release genuinely does not exist. **Do not conclude a
deployment is gone from this endpoint during a suspected outage.** Confirm against the cluster
directly (step 2). Fixing this is action item 1 of drill 2 and is still open.

## 1. Confirm the direction of the failure

Check whether the orchestrator process is up but its dependency is down:

```bash
curl -s localhost:8000/healthz
```

`{"status":"ok"}` proves only that the Python process is alive. It stays green with the entire
cluster down, so it does not rule out this incident — it rules out a crashed orchestrator.

## 2. Confirm the cluster is the problem

```bash
kubectl --context kind-nf-orchestrator get nodes
```

An unreachable control plane looks like this:

```
Unable to connect to the server: dial tcp 127.0.0.1:64114: connectex: No connection could be
made because the target machine actively refused it.
```

If `kubectl` answers normally, this is not the incident — go to
[deployment not reaching INSTANTIATED](deployment-not-reaching-instantiated.md).

## 3. Check whether the node container is running

```bash
docker ps --filter name=nf-orchestrator-control-plane
kind get clusters
```

If `kind get clusters` lists `nf-orchestrator` but `docker ps` shows no container, the node is
stopped, not deleted. That is recoverable without losing any release.

## 4. Restart the node

```bash
docker start nf-orchestrator-control-plane
```

Then wait for the API server rather than assuming:

```bash
until kubectl --context kind-nf-orchestrator get nodes >/dev/null 2>&1; do :; done
kubectl --context kind-nf-orchestrator get nodes
```

Observed on 2026-08-20: the API server answered about 5 seconds after `docker start`, and the node
returned `Ready` with releases intact.

## 5. Expect a noisy recovery window, and wait it out

For roughly 10 seconds after the node returns, pods may report a container that cannot start:

```
{"state":"INSTANTIATED","helm_status":"deployed",
 "pods":[{"phase":"Running","reason":"CreateContainerConfigError"}]}
```

```bash
kubectl --context kind-nf-orchestrator get events \
  --field-selector involvedObject.kind=Pod --sort-by=.lastTimestamp | tail
```

`Error: services have not yet been read at least once, cannot construct envvars` is kubelet
catching up after the restart. It resolves itself. Do not redeploy, and do not trust the
`INSTANTIATED` in that response either — the reconciler reports `INSTANTIATED` even while that
container is failing to start (drill 2, finding 2).

## 6. Verify the workload for real

```bash
kubectl --context kind-nf-orchestrator get pods -l app=<release> \
  -o custom-columns=NAME:.metadata.name,PHASE:.status.phase,READY:.status.containerStatuses[0].ready
helm list --kube-context kind-nf-orchestrator
```

`READY=true` on every expected pod, and the release listed as `deployed`, is the real all-clear.
The orchestrator's own state endpoint is confirmation, not evidence, until action items 1 and 2 of
drill 2 are closed.

## 7. Redeploy anything that was rejected during the outage

Deploy attempts made during the outage did not partially apply — Helm failed before contacting the
cluster, returning `502` and incrementing
`deployments_total{result="failed"}`. Resubmit those intents unchanged.

```bash
curl -s http://localhost:8000/metrics | grep 'result="failed"'
```

## What this runbook does not cover

- A control plane that is running but unhealthy (etcd corruption, expired certificates). Not drilled
  yet; `docker start` will not fix it.
- Recreating the cluster from scratch (`kind create cluster --config kind-config.yaml`). That loses
  every release and is not incident response — it is starting over.
