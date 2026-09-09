# Daily log

A dated record of building this project: what I set out to do each day, what I actually did, and
what I learned. Newest entries first.

| Date | Day | Focus | Log |
|---|---|---|---|
| 2026-09-09 | 19 | Build the boundary the LLM sits behind before building anything that calls a model | [2026-09-09](2026-09-09.md) |
| 2026-09-04 | 18 | Finish M5 with Trivy, harden the stand-in NF against its own findings, and pin the Kubernetes matrix | [2026-09-04](2026-09-04.md) |
| 2026-09-03 | 17 | Build the Gitleaks and banned-terms CI gates, and find that both of them lied on the first attempt | [2026-09-03](2026-09-03.md) |
| 2026-09-02 | 16 | Get kind into CI so the M4 failure drills run on every push instead of only on this laptop | [2026-09-02](2026-09-02.md) |
| 2026-09-01 | 15 | Split the network function's state from the last operation's, closing the final M4 drill finding | [2026-09-01](2026-09-01.md) |
| 2026-08-27 | 14 | Decide whether the deploy engine forces apply conflicts, and find that the desired replica count had been read from a rejected intent | [2026-08-27](2026-08-27.md) |
| 2026-08-26 | 13 | Answer what a stable lifecycle state means, prove it with real alert rules, and find a bug that made the day-12 runbook wrong | [2026-08-26](2026-08-26.md) |
| 2026-08-21 | 12 | Fix what the drills found, in code with tests, and re-run both drills against the fixed build | [2026-08-21](2026-08-21.md) |
| 2026-08-20 | 11 | Run failure drills 1 and 2 against the real cluster and write the postmortems and runbooks from what they turned up | [2026-08-20](2026-08-20.md) |
| 2026-07-24 | 10 | Instrument one real metric, scrape it with Prometheus during real deploys, and see it in Grafana | [2026-07-24](2026-07-24.md) |
| 2026-07-23 | 9 | Build the M3 reconciler and watch the real lifecycle-state sequence it derives from cluster signals | [2026-07-23](2026-07-23.md) |
| 2026-07-22 | 8 | Build the M2 deploy engine and prove a validated intent reaches a Running pod on kind | [2026-07-22](2026-07-22.md) |
| 2026-07-21 | 7 | Build the M1 intent-validation endpoint and document it from what it actually returned | [2026-07-21](2026-07-21.md) |
| 2026-07-20 | 6 | Understand CI by reading every real job and proving I can break it; close out Phase A | [2026-07-20](2026-07-20.md) |
| 2026-07-17 | 5 | Observe the real state sequence a deployment goes through and reconcile it with the lifecycle diagram | [2026-07-17](2026-07-17.md) |
| 2026-07-16 | 4 | Deploy a workload with Helm onto kind by hand and watch it reach Running | [2026-07-16](2026-07-16.md) |
| 2026-07-15 | 3 | Exercise intent validation and capture real rejection errors | [2026-07-15](2026-07-15.md) |
| 2026-07-14 | 2 | Run the FastAPI app for real and document what actually happens | [2026-07-14](2026-07-14.md) |
| 2026-07-13 | 1 | Set up the handbook repo and record why it exists | [2026-07-13](2026-07-13.md) |
