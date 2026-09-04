# ADR-0011: Scan thresholds, and hardening the stand-in NF

- **Status:** Accepted
- **Date:** 2026-09-04
- **Completes:** M5's third gate, alongside
  [ADR-0010](0010-leak-gates-must-not-leak.md)'s Gitleaks and banned-terms jobs.

## Context

M5 promised Trivy. Trivy answers two different questions and this project needs both:

- **`trivy config`** reads the Helm chart for insecure defaults. Run against
  `charts/stand-in-nf` as it stood, it produced **2 HIGH, 4 MEDIUM and 11 LOW** findings — a
  container running as root, with a writable root filesystem, no seccomp profile, no dropped
  capabilities, no resource bounds, binding a privileged port.
- **`trivy fs --scanners vuln`** reads the dependency set for known CVEs. Run against the repository,
  it produced this:

  ```
  ┌────────┬──────┬─────────────────┐
  │ Target │ Type │ Vulnerabilities │
  ├────────┼──────┼─────────────────┤
  │   -    │  -   │        -        │
  └────────┴──────┴─────────────────┘
  Legend: '-': Not scanned
  ```

  Nothing was scanned. `pyproject.toml` lists three unpinned dependencies and there is no lockfile,
  so Trivy had no file it could read. A dependency scanner with no target reports clean forever —
  the same failure mode ADR-0010 rejected for the banned-terms gate one day earlier, arriving from a
  different direction.

There is also an open question from day 16: the e2e job ran whatever Kubernetes version
`helm/kind-action` defaulted to (v1.35.0) while every M4 drill was verified on v1.36.1. It passed,
but nothing had asserted it should.

## Decision

**Fail the build on HIGH and CRITICAL; print everything.** Both scans run with
`--severity HIGH,CRITICAL --exit-code 1`. MEDIUM and LOW findings appear in the job log without
failing it. A threshold that fails on everything gets suppressed wholesale the first time it is
inconvenient; one that fails on nothing is decoration. The accepted findings are listed below rather
than hidden in an ignore file, so raising the bar later is a decision about a known list.

**The stand-in NF is hardened rather than exempted.** The chart now uses
`nginxinc/nginx-unprivileged`, which runs as UID 101 and listens on 8080, so the pod needs neither
root nor the privileged port range. On top of that: `runAsNonRoot`, an explicit UID/GID/fsGroup, the
`RuntimeDefault` seccomp profile, `allowPrivilegeEscalation: false`, all capabilities dropped, a
read-only root filesystem, and CPU/memory requests and limits.

A read-only root filesystem needs somewhere to write, so `/tmp` and `/var/cache/nginx` are
`emptyDir` mounts — nginx keeps its pid file and client-body buffers there and nowhere else. This is
the part that would have broken silently if the chart had been hardened without deploying it.

**Trivy reads the dependency set CI actually installs.** The `vuln-scan` job runs
`pip install -e ".[dev]"`, then `pip freeze` into `.trivy-deps/requirements.txt`, then scans that.
No lockfile is committed. The consequence is deliberate and worth stating: because the dependencies
are unpinned, the scan describes *today's* resolution, so a run can start failing without this
repository changing. That is the honest behaviour for a project that does not pin. Pinning is a
separate decision, not made here.

**The e2e job runs a pinned Kubernetes matrix.** `v1.34.0` and `v1.35.0`, both as explicit
`kindest/node` images, `fail-fast: false`. Cross-version support is now asserted rather than
inherited from a tool's default.

### Accepted findings

| Finding | Severity | Why it stands |
|---|---|---|
| KSV-0125 restrict images to trusted registries | MEDIUM | The image comes from Docker Hub. A private registry is infrastructure this project does not have, and pretending otherwise would mean a fake allowlist |
| KSV-0020 / KSV-0021 runs with UID / GID ≤ 10000 | LOW | UID 101 is the identity `nginx-unprivileged` ships with and its files are owned by. Overriding it to clear a LOW finding risks a container that cannot read its own config |
| KSV-0110 workloads in the default namespace | LOW | Single-namespace is an explicit non-goal in [the overview](../00-overview.md). Fixing this finding would mean building tenancy the project says it does not have |

KSV-0125 is new: it appeared *because* of the hardening, since the unprivileged image comes from a
different Docker Hub path. Hardening moved the finding rather than removing it, which is worth
noticing.

## Verified on 2026-09-04

Chart, before and after:

```
before: {'HIGH': 2, 'MEDIUM': 4, 'LOW': 11}
after:  {'MEDIUM': 1, 'LOW': 3}
```

Cleared: read-only root filesystem, default security context, privilege escalation, running as root,
seccomp, privileged ports, capabilities (three findings), CPU and memory requests and limits.

The hardened chart deployed and ran, which is the check that matters more than the scan:

```
$ helm upgrade --install harden-probe ./charts/stand-in-nf --set-json '{"replicaCount":2,...}'
STATUS: deployed
  2/2 ready after ~21s

$ kubectl exec harden-probe-... -- id
uid=101(nginx) gid=101(nginx) groups=101(nginx)

$ kubectl exec harden-probe-... -- sh -c 'touch /etc/probe'
  rootfs write refused (good)
```

All 6 end-to-end tests still pass against it, unchanged.

Dependency scanning, before and after giving Trivy something to read:

```
before:  Target '-'   Type '-'   Vulnerabilities '-'      (not scanned)
after:   requirements.txt   pip   0                        (35 packages)
```

And, because a scanner nobody has watched fail is not a verified scanner, against
`urllib3==1.24.1`, `requests==2.19.1`, `jinja2==2.10`:

```
requirements.txt (pip)  8 vulnerabilities
  jinja2   CVE-2019-10906  HIGH   2.10 -> 2.10.1
  urllib3  CVE-2023-43804  ...
exit code with --exit-code 1: 1
```

## Consequences

- **Good:** the deployed workload is now a plausible one. A stand-in NF that runs as root with a
  writable filesystem teaches the wrong lesson about what deploying a network function looks like.
- **Good:** the dependency gate scans something, and the difference between "clean" and "did not
  look" is visible in the report's own output.
- **Good:** cross-version support is asserted by the matrix instead of being a lucky default.
- **Cost:** the chart is markedly more complex — a securityContext block, two volumes and two mounts
  for what is still an nginx pod. That complexity is the point, but it is real.
- **Cost:** `readOnlyRootFilesystem` is the kind of setting that breaks a workload in production and
  not in a smoke test. It was verified by deploying and writing to `/etc`; a future chart change
  needs the same check.
- **Cost:** the scan result is not reproducible across time, because unpinned dependencies resolve
  differently. A green run today says nothing about tomorrow.
- **Cost:** `make trivy` needs `MSYS_NO_PATHCONV=1` to survive Git Bash on Windows rewriting the
  container-side paths. Harmless elsewhere, but it is a wart in a Makefile.

## Alternatives considered

- **Fail on MEDIUM too, with a `.trivyignore`.** Rejected for now: the four accepted findings are
  each a deliberate scope decision, and a table in an ADR explains them better than an ignore file
  with a comment. Worth revisiting once the list stops being all-scope-decisions.
- **Keep plain `nginx` and suppress the findings.** Rejected: the findings are correct. A stand-in
  that ignores them models a workload nobody should ship.
- **Commit a lockfile so the scan is reproducible.** Deferred rather than rejected. It is the right
  answer for reproducibility, but it changes how the project installs dependencies and deserves its
  own decision instead of arriving as a side effect of adding a scanner.
- **Scan the nginx image itself with `trivy image`.** Rejected for now: the base image is upstream
  and not built here, so a finding would have no action attached to it beyond bumping a tag.
- **Pin the e2e Kubernetes version to v1.36.1 to match local development.** Rejected: it would make
  CI agree with one laptop. The matrix is worth more, and the gap it leaves — CI covers 1.34 and
  1.35, development happens on 1.36.1 — is now written down instead of unnoticed.
