# Runbook — control plane unreachable

**Use when:** requests that need the cluster return `503 kubernetes cluster unreachable`, or
`/readyz` is failing.

**Severity:** Sev-2. The orchestrator is unavailable and says so, which is the correct behaviour.

Every command here was run during
[drill 2](../postmortems/2026-08-20-drill-2-control-plane-unreachable.md) on 2026-08-20, and
re-run against the fixed build on 2026-08-21.

## How this incident presents (since 2026-08-21)

Every route that needs the cluster returns **503** with the underlying error:

```bash
curl -s -w ' http=%{http_code}' localhost:8000/deployments/<release>
```

```
{"detail":"Error: kubernetes cluster unreachable: Get \"https://127.0.0.1:64114/version\": ..."} http=503
```

`503` means "I could not ask the cluster". `502` means the cluster answered and refused — a
different incident, not this one.

The detail tells you which kind of unreachable. A **refused** connection (the node is stopped)
fails in under a second with `connection refused`. A **frozen** control plane (the node is up but
not answering) fails after the read bound with `helm status did not answer within 3s` —
[drill 6](../postmortems/2026-09-13-drill-6-frozen-control-plane.md). A frozen node shows as running
in `docker ps`, so step 3 below will not find it stopped: check `docker inspect -f
'{{.State.Paused}}'`, and for a real cluster, whether the API server is overloaded rather than down.

**Two alerts cover this, and they are not interchangeable.** `ClusterUnreachable` fires when the
orchestrator is up and cannot reach the cluster. `OrchestratorScrapeFailing` fires when Prometheus
cannot scrape the orchestrator at all — and while it fires, `ClusterUnreachable` goes *inactive*
because its series is stale, so an alert clearing is not evidence of recovery.

> **Before 2026-08-21** this endpoint returned HTTP 200 with
> `{"state":"NOT_INSTANTIATED","helm_status":null,"pods":[]}` during an outage, identical to a
> genuine teardown, and `DELETE` returned `{"uninstalled": false}` for a release that was still
> running. If you are reading output from an older build, do not believe it. Fixed in
> [ADR-0006](../../design/adr/0006-instantiated-means-the-intent-is-satisfied.md).

## 1. Confirm the direction of the failure

```bash
curl -s -w ' http=%{http_code}' localhost:8000/healthz
```

```bash
curl -s -w ' http=%{http_code}' localhost:8000/readyz
```

`/healthz` is liveness only: `{"status":"ok"}` proves the Python process is alive and nothing more.
It stays green with the entire cluster down, deliberately.

`/readyz` is the one that answers this question. `503` there confirms the orchestrator cannot reach
the cluster; `200` means the problem is elsewhere and this is not your runbook.

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
```

```bash
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
```

Observed twice: the API server answered about 5 seconds after `docker start` (20:50:31 on 08-20,
16:14:29 on 08-21), with releases intact both times.

## 5. Expect a noisy recovery window, and wait it out

For roughly 10 seconds after the node returns, kubelet may not be able to start containers yet.
On 2026-08-20 that surfaced as a pod whose container could not be configured:

```bash
kubectl --context kind-nf-orchestrator get events --field-selector involvedObject.kind=Pod --sort-by=.lastTimestamp | tail
```

```
Warning  Failed   pod/drill-one-...  Error: services have not yet been read at least once,
                                     cannot construct envvars
Warning  BackOff  pod/drill-one-...  Back-off restarting failed container nf
Normal   Started  pod/drill-one-...  Container started
```

This resolves itself — do not redeploy. The state endpoint reports the window honestly now, as
`INSTANTIATING` with `ready_replicas` below `desired_replicas`, because readiness rather than pod
phase decides `INSTANTIATED`. Before 2026-08-21 it reported `INSTANTIATED` throughout, with
`CreateContainerConfigError` sitting visibly in the same response.

## 6. Verify the workload for real

```bash
kubectl --context kind-nf-orchestrator get pods -l app=<release> -o custom-columns=NAME:.metadata.name,PHASE:.status.phase,READY:.status.containerStatuses[0].ready
```

```bash
helm list --kube-context kind-nf-orchestrator
```

`READY=true` on every expected pod, and the release listed as `deployed`, is the all-clear. The
orchestrator now agrees when that is true, and shows the numbers it used:

```bash
curl -s localhost:8000/deployments/<release>
```

```
{"release":"drill-one","state":"INSTANTIATED","helm_status":"deployed",
 "desired_replicas":3,"ready_replicas":3,"pods":[...]}
```

## 7. Redeploy anything that was rejected during the outage

Deploy attempts made during the outage did not partially apply — Helm failed before changing
anything, returning `503` and incrementing `deployments_total{result="failed"}`. Resubmit those
intents unchanged. Teardowns attempted during the outage also returned `503` and did nothing.

```bash
curl -s localhost:8000/metrics | grep 'result="failed"'
```

## What this runbook does not cover

- A control plane that is running but unhealthy (etcd corruption, expired certificates). Not
  drilled yet; `docker start` will not fix it.
- Recreating the cluster from scratch (`kind create cluster --config kind-config.yaml`). That loses
  every release and is not incident response — it is starting over.
