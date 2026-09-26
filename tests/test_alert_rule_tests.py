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
