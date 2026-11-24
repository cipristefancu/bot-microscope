"""Static guards: fabricated defaults must not come back, and automation must stay opt-in."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXTENSION = PROJECT_ROOT / "browser-extension"

# Files that read or write captured profiles. capture.py is the one place that may name the
# placeholder numbers, because it exists to recognise and remove them.
GUARDED_FILES = [
    EXTENSION / "parser.js",
    EXTENSION / "content.js",
    EXTENSION / "popup.js",
    PROJECT_ROOT / "src" / "botaudit" / "web.py",
    PROJECT_ROOT / "index.html",
]

# Numbers older builds invented, and the code shapes that invented them.
FORBIDDEN = {
    "placeholder following count 2150": re.compile(r"\b2150\b"),
    "placeholder following count 1850": re.compile(r"\b1850\b"),
    "suspect-handle ternary fill": re.compile(r"isSuspect\s*\?"),
    "nullish default for followers/following": re.compile(r"\?\?\s*(15|1800)\b"),
    "python default for followers/following": re.compile(
        r"[\"'](followers_count|following_count)[\"']\s*:\s*(15|1800)\b"
    ),
    "numeric fallback for a profile number": re.compile(
        r"(post_count|followers_count|following_count|bio_length)\s*"
        r"(\|\||\?\?)\s*\d+"
    ),
}


def _code_lines(path: Path) -> list[tuple[int, str]]:
    lines = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith(("//", "#", "*", "/*")) or "LEGACY_PLACEHOLDER_TUPLES" in line:
            continue
        lines.append((number, line))
    return lines


@pytest.mark.parametrize("path", GUARDED_FILES, ids=lambda p: p.name)
def test_no_invented_numbers_in_capture_code(path: Path) -> None:
    problems = [
        f"{path.name}:{number}: {why}: {line.strip()}"
        for number, line in _code_lines(path)
        for why, pattern in FORBIDDEN.items()
        if pattern.search(line)
    ]
    assert not problems, "Invented profile numbers found:\n" + "\n".join(problems)


def test_the_guard_itself_catches_the_old_code_shapes() -> None:
    old_shapes = [
        "post_count: isSuspect ? 0 : 22,",
        "following_count: parsedStats.following_count !== null ? x : isSuspect ? 2150 : 260,",
        "Math.max(0, parseInt(p.followers_count ?? 15, 10) || 0),",
        '"followers_count": 15,',
        "const flwing = Number(profile.following_count ?? 1800);",
        "p.following_count = 2150;",
    ]
    for line in old_shapes:
        assert any(pattern.search(line) for pattern in FORBIDDEN.values()), line


def test_automatic_scrolling_is_off_by_default_and_gated() -> None:
    js = (EXTENSION / "content.js").read_text(encoding="utf-8")
    assert 'localStorage.getItem(AUTOMATION_KEY) === "1"' in js  # only an explicit opt-in turns it on
    for header in (
        "async function runCollectionLoop(mode) {",
        "async function jumpToNextSearchSlice(explicitSlice) {",
    ):
        assert "isAutomationEnabled()" in js.split(header, 1)[1][:260], header


def test_manifest_no_longer_advertises_the_automatic_modes() -> None:
    manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))
    assert "Dual-Mode" not in manifest["description"]
    assert "OFF by default" in manifest["description"]
    assert manifest["version"] != "2.0.0"
