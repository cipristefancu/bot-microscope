"""Tests for Extension HUD Glassmorphism UX, Forensic Bento Inspector & Dual-Action Mark+Close."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXT_DIR = PROJECT_ROOT / "browser-extension"
BG_CONTRACT = PROJECT_ROOT / "tests" / "js" / "background_contract.js"
HUD_CONTRACT = PROJECT_ROOT / "tests" / "js" / "hud_inspector_contract.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_all_extension_js_files_have_valid_syntax() -> None:
    """Every JavaScript file in browser-extension/ passes Node syntax check."""
    js_files = sorted(EXT_DIR.glob("*.js"))
    assert len(js_files) >= 5
    for js_file in js_files:
        proc = subprocess.run(
            ["node", "--check", str(js_file)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=20,
        )
        assert proc.returncode == 0, f"Syntax error in {js_file.name}: {proc.stderr}"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_mark_bot_human_unsure_saves_backend_data_and_closes_active_tab() -> None:
    """Each of [Mark BOT], [Mark HUMAN], and [?] saves checkpoint + label, then closes the tab."""
    done = subprocess.run(
        ["node", str(BG_CONTRACT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=30,
    )
    results: dict[str, Any] = json.loads(done.stdout)

    for idx, label in enumerate(("bot", "human", "unsure")):
        entry = results[label]
        expected_tab_id = 101 + idx

        # Task 1A: Observed profile numbers saved to /api/checkpoint first
        assert entry["fetchEndpoints"] == [
            "http://127.0.0.1:8000/api/checkpoint",
            "http://127.0.0.1:8000/api/labels",
        ]
        assert entry["checkpointBody"]["artist"] == "target_alpha"
        saved_prof = entry["checkpointBody"]["profiles"][0]
        assert saved_prof["username"] == "reea.404"
        assert saved_prof["post_count"] == 49
        assert saved_prof["followers_count"] == 1515
        assert saved_prof["following_count"] == 513
        assert saved_prof["manual_label"] == label

        # Task 1B: Ground-truth label note persisted to /api/labels
        assert entry["labelBody"]["username"] == "reea.404"
        assert entry["labelBody"]["label"] == label

        # Task 2: Active browser tab closed automatically
        assert entry["resp"]["ok"] is True
        assert entry["resp"]["savedServer"] is True
        assert entry["resp"]["closedTab"] is True
        assert entry["removedTabs"] == [expected_tab_id]

    # Even if local backend is offline, active tab still closes cleanly
    offline = results["offline"]
    assert offline["resp"]["ok"] is True
    assert offline["resp"]["savedServer"] is False
    assert offline["removedTabs"] == [404]

    # Even if backend takes >600ms/call, tab closes in <450ms while Service Worker completes persistence
    slow = results["slowServer"]
    assert slow["elapsedToCloseMs"] < 450
    assert slow["resp"]["ok"] is True
    assert slow["resp"]["closedTab"] is True
    assert slow["resp"]["pendingServer"] is True
    assert slow["removedTabsDuringClose"] == [505]
    assert slow["backgroundFetchEndpoints"] == [
        "http://127.0.0.1:8000/api/checkpoint",
        "http://127.0.0.1:8000/api/labels",
    ]
    assert slow["backgroundCheckpointBody"]["skip_live_counts"] is True
    assert slow["backgroundCheckpointBody"]["defer_workers"] is True


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_forensic_bento_inspector_renders_ratios_gauges_and_guards_shortcuts() -> None:
    """Verify 3-tile metric bento, 2,383x ratio pill, 60p gauge, singular labels & shortcut safety."""
    done = subprocess.run(
        ["node", str(HUD_CONTRACT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=30,
    )
    out: dict[str, Any] = json.loads(done.stdout)

    # 1. Extreme mass-following active bot (@kamchor_shyr_143: 1 post, 2 flwrs, 4,766 flwing -> 15p burst + 60p ratio cap = 75p)
    assert out["kamchorScore"] == 75
    k_html = out["kamchorHtml"]
    assert "botaudit-metric-bento" in k_html
    assert '<span class="botaudit-metric-lbl">Post</span>' in k_html  # singular for 1 post!
    assert "4,766" in k_html
    assert "2,383×" in k_html  # live follow/follower imbalance ratio
    assert "botaudit-handle-digits" in k_html  # 143 highlighted
    assert 'style="width:75%;' in k_html  # 75/100 score gauge
    assert "botaudit-gauge-cutoff" in k_html  # 60p cutoff marker
    assert "Bio <b>51c</b>" in k_html
    assert "heart touching shyr" in k_html
    assert '<kbd class="botaudit-kbd">B</kbd>' in k_html
    assert '<kbd class="botaudit-kbd">H</kbd>' in k_html
    assert '<kbd class="botaudit-kbd">U</kbd>' in k_html

    # 2. Clean private human (@reea.404: 49 posts, 1,515 followers, 513 following -> 0p)
    assert out["reeaScore"] == 0
    r_html = out["reeaHtml"]
    assert "0.3×" in r_html
    assert "0 bot clues triggered — clean profile" in r_html

    # 3. Unobserved / partial profile never invents numbers or fake ratios
    p_html = out["partialHtml"]
    assert '<span class="botaudit-metric-val">—</span>' in p_html
    assert "ratio" not in p_html

    # 4. Transient preview fields stripped before checkpoint / CSV persistence
    assert out["strippedHasAvatar"] is False
    assert out["strippedHasBioPreview"] is False
    assert out["strippedFollowing"] == 4766

    # 5. Manually confirmed bot always renders CONFIRMED BOT pill & alert card even below 60p
    conf_html = out["confirmedDressedBotHtml"]
    assert "CONFIRMED BOT" in conf_html
    assert "botaudit-inspect-bot" in conf_html
    assert "botaudit-score-bot" in conf_html

    # 6. Single-key shortcuts never fire inside inputs, textareas, or contenteditable boxes
    assert all(out["guardChecks"].values()), out["guardChecks"]


def test_glassmorphism_styles_and_compact_hud_structure_present() -> None:
    """Verify elevated Glassmorphism CSS rules and dual-action HUD hooks exist."""
    css = (EXT_DIR / "content.css").read_text(encoding="utf-8")
    js = (EXT_DIR / "content.js").read_text(encoding="utf-8")
    popup = (EXT_DIR / "popup.html").read_text(encoding="utf-8")

    # Frosted glass blur + saturation + specular inset border + forensic bento selectors
    assert "backdrop-filter: blur(24px) saturate(180%)" in css
    assert "-webkit-backdrop-filter: blur(24px) saturate(180%)" in css
    assert "width: 336px" in css
    assert ".botaudit-inspect-card" in css
    assert ".botaudit-metric-bento" in css
    assert ".botaudit-forensic-strip" in css
    assert ".botaudit-score-gauge" in css
    assert ".botaudit-gauge-cutoff" in css
    assert ".botaudit-badge-suspect" in css
    assert ".botaudit-verdict-bot" in css
    assert ".botaudit-verdict-human" in css
    assert ".botaudit-verdict-unsure" in css
    assert ".botaudit-tools-toggle" in css

    # Dual-action handler, keyboard shortcuts & cross-tab live storage sync in content.js
    assert "MARK_PROFILE_AND_CLOSE" in js
    assert "postLabelAndCloseTab" in js
    assert 'window.addEventListener("storage"' in js
    assert 'window.addEventListener("keydown"' in js
    assert "botaudit-tools-toggle" in js

    # Matching glassmorphic popup
    assert "radial-gradient" in popup
    assert "inset 0 1px 0" in popup
