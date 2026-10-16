# ADR-0023: The tools' versions are checked, not assumed

- **Status:** Accepted
- **Date:** 2026-10-16
- **Closes:** findings 1 to 3 of
  [drill 16](../../operations/postmortems/2026-10-16-drill-16-the-wrong-helm.md)
- **Refines:** [ADR-0022](0022-a-failed-spawn-is-classified-by-its-cause.md), which checked that
  helm and kubectl exist; and relies on [ADR-0004](0004-helm-as-the-deploy-mechanism.md) and
  [ADR-0008](0008-forcing-field-ownership-is-an-explicit-operation.md)

## Context

The orchestrator depends on two behaviours that only Helm 4 has. Its `--set-json` accepts a whole
JSON object, which is how the deploy engine passes an intent's values. And its server-side apply
refuses to take a field another manager owns, which is what makes ADR-0008's `409` possible. The
README and CI have pinned Helm 4 since M5, but only by convention: nothing at runtime checked which
helm was installed.

Drill 16 installed Helm 3.19.0, with the cluster healthy. Every deploy and repair failed with
`Error: failed parsing --set-json data …`. Nothing classified that text, so it became a plain
refusal: HTTP 502, which this API documents as "the cluster answered and refused". The cluster had
never been asked. `/readyz` answered 200, `nf_orchestrator_misconfigured` read 0, and reads kept
working, so the orchestrator looked healthy to everything that checks it.

## Decision

**1. Helm's major version is a precondition, checked at runtime.** `helm version --short` is
parsed. A major other than 4 is `HostMisconfigured` (ADR-0022): HTTP 500, naming the version found
and the version required, with no `Retry-After`.

**2. It is checked wherever it matters:**

- before every write, so nothing is attempted with the wrong helm;
- in `/readyz`, because ready means able to do the job;
- in every scrape, through `nf_orchestrator_misconfigured` and the `OrchestratorMisconfigured` page.

The answer is cached for 60 seconds, so the check costs one process a minute.

**3. Helm refusing the orchestrator's own arguments is the host's problem, never the cluster's.**
The parse error is classified as `HostMisconfigured`. That covers a helm swapped while the cached
answer is still valid.

**4. What can't be read isn't called wrong.** A version string the check can't parse, or a helm that
can't be run, is not reported as a wrong version. The first is not evidence of anything, and the
second is ADR-0022's missing-tool finding.

## Consequences

- A wrong helm now presents as what it is, an installation problem, with the version in the error
  and a page. It stops being reported as a cluster that refuses everything.
- The deploy path pays for a cached `helm version` at most once a minute.
- The version requirement now lives in code (`REQUIRED_HELM_MAJOR`) as well as in CI and the README.
  Changing it means changing all three, plus the e2e tests that assert Helm 4's conflict behaviour.
- kubectl's version is not checked. Its supported skew is relative to the API server, so checking it
  would cost an API call. Drill 16 found no kubectl incompatibility that affected the orchestrator.

## Alternatives considered

**Pass values in a form both Helm 3 and 4 accept.** That would remove the parse failure and expose
the worse failure behind it. Measured in drill 16 with Helm 3.19.0 directly: after `kubectl scale`
set a Deployment to 1 replica, `helm upgrade --set replicaCount=2` reported `STATUS: deployed` and
silently set it back to 2, with no conflict. That is exactly what ADR-0008 promises never happens.
Helm 4 refused the same upgrade with a conflict, which the API answered as `409`. Failing loudly is
the safer outcome.

**Check once at start-up.** That misses a tool upgraded or swapped under a running process, and turns
a misconfiguration into a crash loop that says less than a `500` naming the version.

**Rely on CI and the README.** That is what was in place, and the orchestrator itself could not tell
that the promise had been broken.
