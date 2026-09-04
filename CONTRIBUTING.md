# Contributing / Writing guide

This repo, including its documentation, is written for recruiters and hiring managers who skim.
Every page must earn a 30-second skim and reward a 3-minute read.

## Voice and style

- English, active voice, concise. Cut any word that can be removed without losing meaning.
- No marketing tone, no buzzwords. Prefer concrete numbers, diagrams, and reproducible commands.
- Lead every page with one sentence that says what it is and why it matters.
- Frame everything as a **self-directed learning project**. Write as an individual engineer.

## Definition of Done (a doc is not done until all pass)

1. **Factual** — every claim about the system is true of the code repo at that milestone. Anything
   not yet built is marked `(planned)`.
2. **Clean-room** — passes the leak-scrub checklist below.
3. **Reproducible** — any command or step in the doc has been run and produces the stated result.
4. **Readable** — passes the 20-second "what is this and why care" test.
5. **Committed** — one document per commit, clear message, linked from the relevant index.

## Leak-scrub checklist (run before every commit)

This project is a generic re-implementation built only from public sources. Do not include
anything non-public.

- [ ] No real IP addresses, MAC addresses, or internal hostnames.
- [ ] No private organisation/repository names, internal codenames, or internal URLs.
- [ ] No personal names other than my own as author.
- [ ] No institution, employer, lab, or research-group name anywhere.
- [ ] No internal chart names, service names, namespaces, or config values from any non-public system.
- [ ] No credentials, tokens, keys, or secrets (even ones that look fake).
- [ ] Every O-RAN statement cites a public source (O-RAN SC / O-RAN Alliance published docs), with a link.
- [ ] The "self-directed learning project" framing holds on every page.

## CI gates

CI is two jobs, and triggers on every branch push as well as on pull requests.

- **`lint-test`** runs `ruff check .` and `pytest`. No cluster: the end-to-end tests skip
  themselves unless `NF_E2E=1`.
- **`e2e`** (M5) creates a kind cluster with Helm 4 pinned, then runs `pytest tests/e2e`, which
  asserts the ground the M4 failure drills covered by hand — a deploy reaching `INSTANTIATED`,
  drift outside Helm showing as a shortfall, a conflicting resubmit refused with `409`, repair
  taking the field back, a bad image tag reaching `FAILED`, and idempotent teardown. Run it
  locally with `make up && make e2e`.

- **`secret-scan`** (M5) runs Gitleaks over the full history with `--exit-code 1`. History, not
  the diff: a key committed in July and deleted in August is still in the pack file.
- **`banned-terms`** (M5) greps the tracked files for private terms. The list is **not** in this
  repository — it arrives as the `BANNED_TERMS` repository secret — and a match is reported as a
  location and a list index, never as the term, because a public CI log is exactly where a private
  term must not appear. The gate fails closed: unset means fail, not pass. Design and the reasoning
  in [ADR-0010](docs/design/adr/0010-leak-gates-must-not-leak.md).

- **`vuln-scan`** (M5) runs Trivy twice: `config` over the chart for insecure defaults, and `fs`
  over the dependency set CI actually installed for known CVEs. Both fail on HIGH and CRITICAL and
  print everything else; the accepted MEDIUM and LOW findings are listed with reasons in
  [ADR-0011](docs/design/adr/0011-scan-thresholds-and-a-hardened-stand-in.md). Run it locally with
  `make trivy`.

Third-party actions are pinned to a commit rather than a tag, and scanner images to a digest,
because a tag can be moved onto different code.

Two items of the leak-scrub checklist above are now mechanical — credentials, and any term on the
private list. The rest of it is still a manual step before every commit: no real IPs, no personal
names, the O-RAN citation rule, and the self-directed framing.

### Running the term scan yourself

```bash
gh secret set BANNED_TERMS < your-term-list.txt   # once, for CI
BANNED_TERMS="$(cat your-term-list.txt)" make scan
```

One term per line; `#` starts a comment.

## Architecture decisions

Significant decisions are recorded as ADRs in [`docs/design/adr/`](docs/design/adr/) using the
MADR format. To propose a decision, copy the latest ADR, increment the number, and set the status.
