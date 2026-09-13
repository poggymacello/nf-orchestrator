"""The numbers the front-door documents assert, checked against the repository.

Day 21's publish pass fixed a README claiming 74 unit tests when there were 122. Three
days later it claimed 122 when there were 128. Status prose goes stale on its own and the
author is the person least likely to notice, so the claims that can drift are pinned
here and fail the build when they stop being true.

Each check **requires the claim to be present**. A reworded sentence that no longer
matches must fail rather than pass silently — the same fail-closed rule the banned-terms
gate follows, for the same reason: a check that quietly stops checking looks exactly like
one that passes.
"""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def claimed(pattern: str, text: str, where: str) -> int:
    match = re.search(pattern, text)
    assert match, (
        f"{where} no longer contains a claim matching {pattern!r}. If the sentence was "
        "reworded, update this test; if the claim was removed deliberately, remove the check."
    )
    value = match.group(1).lower()
    return NUMBER_WORDS[value] if value in NUMBER_WORDS else int(value)


def count_test_functions(directory: Path) -> int:
    """Counted from source, not by running pytest — a test that ran the suite to count
    itself would be circular. Exact only while nothing is parametrized, which the next
    test asserts rather than assumes."""
    return sum(
        len(re.findall(r"^def test_", path.read_text(encoding="utf-8"), re.MULTILINE))
        for path in directory.glob("test_*.py")
    )


def test_the_static_count_is_still_exact() -> None:
    """If this fails, a test was parametrized and counting `def test_` now undercounts.
    Change how the counts below are taken before trusting them again."""
    uses = [
        path.name
        for path in (ROOT / "tests").rglob("test_*.py")
        if "parametrize" in path.read_text(encoding="utf-8") and path.name != Path(__file__).name
    ]
    assert uses == [], f"parametrize is now used in {uses}; static counting is no longer exact"


def test_readme_unit_test_count() -> None:
    readme = read("README.md")
    stated = claimed(r"\b(\d+) unit tests\b", readme, "README.md")
    assert stated == count_test_functions(ROOT / "tests"), (
        f"README.md says {stated} unit tests; tests/ defines {count_test_functions(ROOT / 'tests')}"
    )


def test_readme_end_to_end_test_count() -> None:
    readme = read("README.md")
    stated = claimed(r"\b(\d+) end-to-end tests\b", readme, "README.md")
    assert stated == count_test_functions(ROOT / "tests" / "e2e")


def test_readme_adr_count() -> None:
    readme = read("README.md")
    stated = claimed(r"\b(\d+) ADRs\b", readme, "README.md")
    actual = len(list((ROOT / "docs" / "design" / "adr").glob("[0-9][0-9][0-9][0-9]-*.md")))
    assert stated == actual, f"README.md says {stated} ADRs; docs/design/adr holds {actual}"


def ci_jobs() -> int:
    return len(yaml.safe_load(read(".github/workflows/ci.yaml"))["jobs"])


def test_readme_ci_job_count() -> None:
    stated = claimed(r"CI runs\s+(\w+)\s+jobs", read("README.md"), "README.md")
    assert stated == ci_jobs()


def test_overview_ci_job_count() -> None:
    stated = claimed(r"\b(\w+) CI jobs\b", read("docs/design/00-overview.md"), "00-overview.md")
    assert stated == ci_jobs()


def test_a_claim_that_disappears_fails_rather_than_passes() -> None:
    """The fail-closed property, checked directly."""
    import pytest

    with pytest.raises(AssertionError, match="no longer contains a claim"):
        claimed(r"\b(\d+) widgets\b", "this text makes no such claim", "fixture")


def test_committed_config_and_docs_are_valid_utf8() -> None:
    """Drill 6: a config edited on Windows was written as cp1252. PyYAML read it back with
    the same default encoding and reported it valid; Prometheus refused to load it with
    `invalid leading UTF-8 octet`. A check that agrees with the writer proves nothing, so
    this one decodes strictly."""
    bad = []
    for pattern in ("monitoring/*.yml", "charts/**/*.yaml", ".github/workflows/*.yaml", "**/*.md"):
        for path in ROOT.glob(pattern):
            if ".venv" in path.parts:
                continue
            try:
                path.read_bytes().decode("utf-8")
            except UnicodeDecodeError as exc:
                bad.append(f"{path.relative_to(ROOT)} (byte {exc.start})")
    assert bad == [], f"not valid UTF-8: {bad}"
