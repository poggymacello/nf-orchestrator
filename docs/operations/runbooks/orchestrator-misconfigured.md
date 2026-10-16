# Runbook — orchestrator cannot run its tools

**Use when:** `OrchestratorMisconfigured` is firing, `nf_orchestrator_misconfigured` is 1, or a route
returns `500` with a detail saying *helm* or *kubectl* *is not installed or not executable on this
host*, *not found on PATH*, or *is installed but Helm 4 is required*.

**Severity:** Sev-1. Every deploy, status and teardown call fails until it is fixed, and nothing
recovers on its own. The cluster is not the problem: do not change anything there.

Every command here was run during
[drill 15](../postmortems/2026-10-16-drill-15-the-orchestrator-cannot-spawn-its-tools.md) on
2026-10-16, with the orchestrator in its drill container
([`manifests/drill.Dockerfile`](../manifests/drill.Dockerfile)) and helm moved off its `PATH`, and
during [drill 16](../postmortems/2026-10-16-drill-16-the-wrong-helm.md) with Helm 3 installed instead
of Helm 4. Steps not exercised there are marked **(not drilled)**.

## How this incident presents

```bash
curl -s -w ' http=%{http_code}' localhost:8000/readyz
```

```
{"detail":"helm not found on PATH on this host: install or fix PATH; retrying will not help"} http=500
```

```bash
curl -s localhost:8000/metrics | grep -E '^nf_(cluster_reachable|orchestrator_misconfigured)'
```

```
nf_orchestrator_misconfigured 1.0
```

There is no `nf_cluster_reachable` sample at all: the orchestrator could not ask, so it makes no
claim about the cluster. `ClusterUnreachable` does not fire. Before 2026-10-16 it did, for a healthy
cluster, and `/readyz` answered `200` because kubectl was still present.

`500`, not `503`, and no `Retry-After`: retrying will not change anything.

## 1. Find out which tool, and where the orchestrator looks for it

The `/readyz` detail names every missing tool; in the drill that was all it took. To see what the
orchestrator's own environment resolves, run this where the orchestrator runs (inside its container,
for a containerised one) **(not drilled)**:

```bash
command -v helm kubectl; echo "PATH=$PATH"
```

The orchestrator finds its tools on its own `PATH`, which is not necessarily the `PATH` of the shell
you are logged in with.

## 2. Put it back

In the drill, restoring the binary was enough. No restart was needed, because each call looks the
tool up afresh:

```bash
mv /usr/local/bin/helm.missing /usr/local/bin/helm
```

On a real host this means installing the pinned version: Helm **4.2.2** (CI and the drill image
both pin it), and a kubectl within one minor version of the cluster. The drill image's
`HELM_VERSION` and `KUBECTL_VERSION` arguments show checksum-verified installs.
**(a fresh install outside the drill image not drilled)**

## 3. Verify

```bash
curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/readyz
curl -s localhost:8000/metrics | grep -E '^nf_(cluster_reachable|orchestrator_misconfigured)|reason="host"'
```

```
200
nf_cluster_reachable 1.0
nf_orchestrator_misconfigured 0.0
nf_releases_unreported{reason="host"} 0.0
```

Seen within one scrape of the binary returning. `OrchestratorMisconfigured` resolves once the gauge
has read 0 for the rule's evaluation.

## When it is the wrong helm, not a missing one

```
{"detail":"helm v3.19.0+g3d8990f is installed but Helm 4 is required (server-side apply that refuses
conflicting field owners, and --set-json taking a whole object): install Helm 4; retrying will not help"} http=500
```

The orchestrator needs Helm **4**. Helm 3 cannot parse the values the orchestrator passes, and with
values it can parse it would silently overwrite fields another manager owns, the opposite of the
`409` this API promises. Drill 16 measured both. Reads keep working under Helm 3, which is why the
orchestrator now checks the version instead of waiting for a deploy to fail.

Install Helm 4 (CI and the drill image pin 4.2.2) in place of the old binary. The orchestrator
re-reads the version at most a minute later; `/readyz` turning `200` and
`nf_orchestrator_misconfigured 0` confirm it. **(replacing the binary under a running orchestrator
not drilled; drill 16 used a separate image per version)**

## What this runbook does not cover

- **A Helm 4 minor or patch version that behaves differently.** Only the major version is checked.
  **(not drilled)**
- **A tool that is present but cannot reach anything** (no kubeconfig, wrong context). That reads as
  the cluster's answer, and belongs to [control plane unreachable](control-plane-unreachable.md).
- **No process slot at all** (`errno 11`, a `503` with `Retry-After`). That is the host's capacity,
  not its configuration; see
  [when the host cannot start threads](scrape-over-budget.md#when-the-host-cannot-start-threads-at-all).
