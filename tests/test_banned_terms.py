import subprocess
import sys
from pathlib import Path

from scan_banned_terms import parse_terms, scan_text

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "scan_banned_terms.py"


def test_parse_terms_handles_newlines_commas_comments_and_case() -> None:
    raw = "Acme-Corp\n# a comment\n\n  Northwind , Initech  \n"
    assert parse_terms(raw) == ["acme-corp", "northwind", "initech"]


def test_parse_terms_of_nothing_is_empty() -> None:
    assert parse_terms("") == []
    assert parse_terms("\n#only a comment\n  \n") == []


def test_a_clean_file_has_no_hits() -> None:
    assert scan_text("a.md", "nothing to see here", ["acme-corp"]) == []


def test_a_match_reports_location_and_index_only() -> None:
    text = "line one\nsomething about Acme-Corp here\n"
    hits = scan_text("docs/a.md", text, ["northwind", "acme-corp"])
    assert hits == [("docs/a.md", 2, 1)]


def test_matching_is_case_insensitive_and_substring() -> None:
    hits = scan_text("a.md", "see ACME-CORPORATION today", ["acme-corp"])
    assert len(hits) == 1


def test_the_reported_hit_never_carries_the_term() -> None:
    """The whole point: a match must not publish the private term into a public log."""
    term = "acme-corp"
    hits = scan_text("a.md", f"mentions {term}", [term])
    assert term not in str(hits)


# --- the CLI, which is what CI actually runs ---


def run_cli(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=SCRIPT.parent.parent,
    )


def test_cli_fails_closed_when_not_configured() -> None:
    """An unconfigured scan is not a passing scan."""
    result = run_cli({"PATH": "", "BANNED_TERMS": ""})
    assert result.returncode == 2
    assert "fails closed" in result.stderr


def test_cli_is_clean_against_a_term_this_repo_does_not_contain(
    monkeypatch: object,
) -> None:
    import os

    env = dict(os.environ)
    env["BANNED_TERMS"] = "a-term-that-appears-nowhere-in-this-repository"
    result = run_cli(env)
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


# --- a path can carry a term just as easily as a line ---


def test_redact_blanks_every_occurrence_case_insensitively() -> None:
    from scan_banned_terms import redact

    assert redact("docs/Acme-Corp/acme-corp.md", ["acme-corp"]) == "docs/***/***.md"


def test_redact_leaves_unrelated_text_alone() -> None:
    from scan_banned_terms import redact

    assert redact("docs/design/adr/0005-x.md", ["acme-corp"]) == "docs/design/adr/0005-x.md"


def test_a_path_named_after_a_term_does_not_leak_it() -> None:
    """Found by running this scanner against a term that appears in a filename."""
    from scan_banned_terms import redact

    path = "docs/build-log/m3-reconciler.md"
    assert "reconciler" not in redact(path, ["reconciler"])
