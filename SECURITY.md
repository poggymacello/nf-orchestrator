# Security policy

## Supported versions

Only `main` is supported. The project has no tagged releases, and fixes land on `main`.

## Reporting a vulnerability

Please report privately, through GitHub's **[private vulnerability reporting](https://github.com/poggymacello/nf-orchestrator/security/advisories/new)**
(the repository's *Security* tab, then *Report a vulnerability*). Do not open a public issue for a
security problem.

Include what you found, how to reproduce it, and what an attacker could do with it. The maintainer
aims to acknowledge reports within seven days. If the report is confirmed, a fix and an advisory follow, with
credit to you unless you'd rather not have it.

## Scope and threat model

nf-orchestrator is a **single-operator lab tool**. It runs on one machine against one Kubernetes
cluster, and its HTTP API has **no authentication or authorisation**. Anyone who can reach the port
can deploy, repair and delete releases in the configured cluster, with the permissions of the
configured kubeconfig. Run it bound to `localhost`, or behind something that authenticates. Reports
that amount to "the unauthenticated API can be called" describe that design and are out of scope.

In scope:

- An intent, or text sent to `/intents/translate`, that reaches Helm or kubectl with anything other
  than validated values (argument injection, values the schema should have rejected).
- Secrets or credentials exposed by the orchestrator: in responses, error details, logs or metrics.
- Supply-chain weaknesses in this repository's CI, dependencies or chart.

## Controls in place

| Control | Where |
|---|---|
| Strict intent validation before any cluster call; no type coercion, no unknown fields | [ADR-0003](docs/design/adr/0003-intent-schema-contract.md) |
| Cluster tools invoked as argument lists, never through a shell | `orchestrator/deploy.py` |
| Free-text input treated as untrusted; translation never deploys | [ADR-0012](docs/design/adr/0012-the-translator-is-untrusted-input.md) |
| Gitleaks over the full git history on every push | `secret-scan` CI job |
| Private-term scan that fails closed and never prints a term | `banned-terms` CI job, [ADR-0010](docs/design/adr/0010-leak-gates-must-not-leak.md) |
| Trivy over the chart (misconfiguration) and the dependencies (CVEs), failing on HIGH and CRITICAL | `vuln-scan` CI job, [ADR-0011](docs/design/adr/0011-scan-thresholds-and-a-hardened-stand-in.md) |
| Third-party CI actions pinned to commit SHAs, scanner images to digests | `.github/workflows/ci.yaml` |
| A Go runtime crash for want of threads reaches callers as its first two lines, not its ~19 KB stack | [ADR-0021](docs/design/adr/0021-a-host-out-of-capacity-is-not-a-slow-host.md) |
