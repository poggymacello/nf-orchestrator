"""The promtool tests in monitoring/tests must test the rules as they are.

promql_expr_test takes a copy of an expression, and a copy goes stale silently: the rule
changes, the test keeps passing against the old text, and the drill it replays is no longer
guarded. CI runs promtool itself (lint-test); this pins the copies to the originals.
"""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
RULES = ROOT / "monitoring" / "nf-lifecycle.rules.yml"
TESTS = sorted((ROOT / "monitoring" / "tests").glob("*.test.yml"))


def rule_expressions() -> set[str]:
    groups = yaml.safe_load(RULES.read_text(encoding="utf-8"))["groups"]
    return {rule["expr"] for group in groups for rule in group["rules"]}


def test_there_are_rule_tests() -> None:
    assert TESTS, "monitoring/tests has no promtool tests"


def test_every_tested_expression_is_a_rule_as_written() -> None:
    known = rule_expressions()
    stale = [
        case["expr"]
        for path in TESTS
        for test in yaml.safe_load(path.read_text(encoding="utf-8"))["tests"]
        for case in test.get("promql_expr_test", [])
        if case["expr"] not in known
    ]
    assert stale == [], f"promtool tests exercise expressions no rule uses any more: {stale}"


def test_the_drill_12_outage_is_replayed() -> None:
    """The case that nearly paged throttling during an outage stays under test."""
    tests = [
        test
        for path in TESTS
        for test in yaml.safe_load(path.read_text(encoding="utf-8"))["tests"]
    ]
    assert any(
        case.get("alertname") == "ClusterThrottlingOrchestrator" and case["exp_alerts"] == []
        for test in tests
        for case in test.get("alert_rule_test", [])
    )


# --- Day 34: every alert is routable and answerable ---

RUNBOOK_BASE = "https://github.com/poggymacello/nf-orchestrator/blob/main/"
SEVERITIES = {"page", "warning"}


def alerts() -> list[dict]:
    groups = yaml.safe_load(RULES.read_text(encoding="utf-8"))["groups"]
    return [rule for group in groups for rule in group["rules"] if "alert" in rule]


def test_every_alert_has_a_known_severity() -> None:
    """Day 34: three rules said `warn` and one said `warning`. An Alertmanager route on
    either value silently missed the others."""
    odd = {a["alert"]: a["labels"].get("severity") for a in alerts()}
    odd = {name: sev for name, sev in odd.items() if sev not in SEVERITIES}
    assert odd == {}, f"severity must be one of {sorted(SEVERITIES)}: {odd}"


def test_every_alert_links_a_runbook_that_exists() -> None:
    broken = {}
    for alert in alerts():
        url = alert.get("annotations", {}).get("runbook_url", "")
        target = ROOT / url.removeprefix(RUNBOOK_BASE) if url.startswith(RUNBOOK_BASE) else None
        if target is None or not target.is_file():
            broken[alert["alert"]] = url or "(none)"
    assert broken == {}, f"runbook_url missing or pointing nowhere: {broken}"


def test_every_runbook_is_reachable_from_an_alert() -> None:
    """A runbook no alert leads to is reached only by someone who already knows it exists.

    One hop is allowed: node-under-disk-pressure is reached from the runbook NFFailed links,
    at the step that finds an evicted pod.
    """
    import re

    folder = ROOT / "docs" / "operations" / "runbooks"
    linked = {a["annotations"]["runbook_url"].rsplit("/", 1)[-1] for a in alerts()}
    one_hop = {
        target
        for name in linked
        for target in re.findall(r"\]\(([\w-]+\.md)", (folder / name).read_text(encoding="utf-8"))
    }
    runbooks = {p.name for p in folder.glob("*.md")}
    unlinked = runbooks - linked - one_hop
    assert unlinked == set(), f"no alert leads to these runbooks: {sorted(unlinked)}"
