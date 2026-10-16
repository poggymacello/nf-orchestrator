"""The reference documentation must describe the code as it is.

Day 34 wrote docs/reference/ from the code. Reference pages that drift are worse than none,
because they are the pages people trust without checking, so each one is checked here
against its source: the settings the code reads, the routes it serves, the series it
emits and the alerts it defines. Every relative link in the repository's Markdown is
checked too, anchors included.
"""

import re
from pathlib import Path

import yaml

from orchestrator import app

ROOT = Path(__file__).resolve().parent.parent
REFERENCE = ROOT / "docs" / "reference"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def source() -> str:
    return "\n".join(read(path) for path in (ROOT / "orchestrator").glob("*.py"))


def test_every_setting_the_code_reads_is_documented() -> None:
    settings = set(re.findall(r'os\.environ\.get\("(NF_[A-Z_]+)"', source()))
    assert settings, "found no NF_* settings in the code; the pattern no longer matches"
    page = read(REFERENCE / "configuration.md")
    missing = sorted(name for name in settings if f"`{name}`" not in page)
    assert missing == [], f"configuration.md does not document {missing}"


def test_every_documented_setting_still_exists() -> None:
    settings = set(re.findall(r'os\.environ\.get\("(NF_[A-Z_]+)"', source()))
    documented = set(re.findall(r"`(NF_[A-Z_]+)`", read(REFERENCE / "configuration.md")))
    assert documented - settings == set(), f"documented but not read: {documented - settings}"


def test_every_route_is_documented() -> None:
    framework = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
    page = read(REFERENCE / "api.md")
    missing = [
        f"{method} {route.path}"
        for route in app.routes
        if route.path not in framework
        for method in sorted(getattr(route, "methods", None) or ())
        if method not in {"HEAD", "OPTIONS"} and f"`{method} {route.path}`" not in page
    ]
    assert missing == [], f"api.md does not document {missing}"


def test_every_series_is_documented() -> None:
    declared = set(re.findall(r'(?:GaugeMetricFamily|Counter)\(\s*"([a-z_]+)"', source()))
    assert declared, "found no metric declarations; the pattern no longer matches"
    page = read(REFERENCE / "metrics.md")
    missing = sorted(name for name in declared if f"`{name}`" not in page)
    assert missing == [], f"metrics.md does not document {missing}"


def test_every_alert_row_matches_its_rule() -> None:
    rules = yaml.safe_load(read(ROOT / "monitoring" / "nf-lifecycle.rules.yml"))["groups"]
    page = read(REFERENCE / "alerts.md")
    wrong = []
    for rule in (r for group in rules for r in group["rules"] if "alert" in r):
        row = next((line for line in page.splitlines() if line.startswith(f"| `{rule['alert']}` |")), "")
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        if len(cells) < 3 or cells[1] != rule["labels"]["severity"] or cells[2] != rule["for"]:
            wrong.append(rule["alert"])
    assert wrong == [], f"alerts.md disagrees with the rules for {wrong}"


def slug(heading: str) -> str:
    """GitHub's anchor for a Markdown heading."""
    text = re.sub(r"[`*_]", "", heading.strip().lower())
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def anchors(path: Path) -> set[str]:
    seen: dict[str, int] = {}
    found = set()
    in_code = False
    for line in read(path).splitlines():
        if line.startswith("```"):
            in_code = not in_code
            continue
        heading = re.match(r"#{1,6} (.+)", line)
        if heading and not in_code:
            base = slug(heading.group(1))
            count = seen.get(base, 0)
            found.add(base if count == 0 else f"{base}-{count}")
            seen[base] = count + 1
    return found


def markdown_files() -> list[Path]:
    return [
        path
        for path in ROOT.rglob("*.md")
        if not {".venv", "node_modules", ".git"} & set(path.parts)
    ]


def test_every_relative_link_resolves() -> None:
    broken = []
    for path in markdown_files():
        # Code is not prose: a regex such as `([-a-z0-9]*)` inside backticks is not a link.
        text = re.sub(r"```.*?```", "", read(path), flags=re.DOTALL)
        text = re.sub(r"`[^`\n]*`", "", text)
        for target in re.findall(r"\]\(([^)\s]+)\)", text):
            if re.match(r"[a-z]+:", target) or target.startswith("mailto:"):
                continue
            file_part, _, anchor = target.partition("#")
            resolved = (path.parent / file_part).resolve() if file_part else path
            if not resolved.exists():
                broken.append(f"{path.relative_to(ROOT)} -> {target}")
            elif anchor and resolved.suffix == ".md" and anchor not in anchors(resolved):
                broken.append(f"{path.relative_to(ROOT)} -> {target} (no such heading)")
    assert broken == [], "broken links:\n" + "\n".join(broken)
