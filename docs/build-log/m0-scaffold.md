# M0 — Scaffold, and learning the mechanics by hand

**Goal:** a repository, a FastAPI skeleton, a kind cluster, and one CI job — plus enough hands-on
time with pydantic, Helm and Kubernetes to build M1 to M3 without guessing.

> **Assembled retrospectively on 2026-09-09**, from the daily log entries for days 1 to 6
> ([2026-07-13](../daily-log/2026-07-13.md) to [2026-07-20](../daily-log/2026-07-20.md)). The other
> build logs were written the day their milestone finished; this one was not written at all, and the
> milestone map promised it for eight weeks. Everything below is taken from those entries or from
> the code — nothing is reconstructed from memory.

## What M0 delivered

- The repository: README, CONTRIBUTING with the leak-scrub checklist, ADR-0001, the milestone map,
  LICENSE, `.gitignore`.
- A FastAPI application in `orchestrator/__init__.py` with one route, `GET /healthz`.
- `kind-config.yaml` and a single-node cluster named `nf-orchestrator` — the same cluster every
  milestone since has reused rather than recreated.
- One CI job, `lint-test`: `ruff check .` and `pytest`, on pushes to `main` and on pull requests.

## The part that was not code

Days 3, 4 and 5 built nothing that shipped. They exercised, by hand, the three mechanisms M1, M2 and
M3 would later automate — and each produced a finding that outlived the exercise.

**Day 3, pydantic** ([2026-07-15](../daily-log/2026-07-15.md)). A throwaway `Intent` model in
`scripts/intent_validate.py`, run against one valid payload and three broken ones, capturing the
real `ValidationError` output: `missing`, `int_parsing`, `literal_error`. The surprise was that
`"replicas": "3"` is *accepted* and coerced, while `"replicas": "two"` is rejected — pydantic v2's
default mode coerces any string that parses. That is why the `Intent` model shipped in M1 sets
`strict=True`.

**Day 4, Helm and kind** ([2026-07-16](../daily-log/2026-07-16.md)). A scratch chart deploying
nginx, installed, upgraded to two replicas, and uninstalled. Then, deliberately, pointed at an image
that does not exist: **`helm upgrade` reported success anyway**, while the pod went `ErrImagePull` →
`ImagePullBackOff`.

**Day 5, lifecycle states** ([2026-07-17](../daily-log/2026-07-17.md)). Watched the same failure at
the release level: `helm status` stayed `STATUS: deployed` through an entirely failed rollout. Also
the day the four lifecycle states were grounded in ETSI NFV SOL003 rather than invented —
`instantiationState` gives `NOT_INSTANTIATED` and `INSTANTIATED`, and `INSTANTIATING` / `FAILED`
collapse SOL003's operation states.

Those two failures are the same fact found twice, and it became the load-bearing idea of M3:
**release status and workload health are separate signals, and only the second tells you the deploy
worked.** It is written down as [ADR-0005](../design/adr/0005-derive-lifecycle-state-from-cluster-signals.md),
and M4's drills spent three days finding the places the reconciler had not taken it seriously enough.

The SOL003 collapse recorded on day 5 was revisited on day 15 and partly undone —
[ADR-0009](../design/adr/0009-the-operation-is-not-the-network-function.md) split the operation
state back out — which is what a decision written down rather than assumed makes possible.

## What broke

**`uvicorn --reload` reloaded nothing** ([2026-07-14](../daily-log/2026-07-14.md)). It logged
`WatchFiles detected changes... Reloading...` and kept serving the old response body. Running plain
`uvicorn orchestrator:app` served the edit immediately, which located the fault in the reload
mechanism rather than the code. The probe edit was reverted afterwards.

**CONTRIBUTING claimed CI gates that did not exist** ([2026-07-20](../daily-log/2026-07-20.md)).
Reading `.github/workflows/ci.yaml` in full showed exactly one job, while CONTRIBUTING described
Gitleaks and a banned-terms grep as enforced. That gap was flagged on day 6 rather than papered
over, marked `(planned, M5)`, and finally closed on day 17 —
[ADR-0010](../design/adr/0010-leak-gates-must-not-leak.md). Eight weeks between noticing and fixing,
with the claim marked honestly the whole time.

## What M0 got wrong, judged from here

**The FastAPI choice was never recorded.** The milestone map cited an `ADR-0002 (FastAPI)` from day
1 to day 21; no such ADR was ever written, and the daily logs contain no evidence the choice was
deliberated — day 2 runs FastAPI, it does not choose it. It has not been written retrospectively,
because [ADR-0001](../design/adr/0001-record-architecture-decisions.md) exists precisely to say that
rationale reconstructed after the fact is lossy, and manufacturing one would contradict the reason
the ADR habit exists. The map now says the decision is unrecorded instead of pointing at a file that
was never going to appear.

**`demo:` in the Makefile still echoes a placeholder** written at M0 — `"wired up in M2 once the
deploy engine exists"`. M2 shipped on day 8.

## Checks

`ruff check .` and `pytest` through the `lint-test` job, which is all CI was at the time. The
cluster was left running and every milestone since has reused it.
