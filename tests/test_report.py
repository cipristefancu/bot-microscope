"""Tests for HTML report generator and CLI pipeline (botaudit.report & run_audit)."""

from __future__ import annotations

import csv
import re
import subprocess
import sys
from pathlib import Path

import pytest

from botaudit.report import (
    FORBIDDEN_WORDS,
    generate_chart_base64,
    render_html_report,
    validate_report_content,
)
from botaudit.stats import ArtistStats


def _make_stat(
    name: str = "TestArtist",
    sample_size: int = 100,
    pct_bot: float = 0.30,
    ci_low: float = 0.22,
    ci_high: float = 0.39,
    mean_score: float = 0.35,
    status: str = "ok",
) -> ArtistStats:
    """Helper to construct an ArtistStats instance."""
    return ArtistStats(
        artist=name,
        sample_size=sample_size,
        mean_bot_score=mean_score,
        pct_bot_like=pct_bot,
        ci_low=ci_low,
        ci_high=ci_high,
        flag_rates={
            "no_photo": 0.20,
            "zero_posts": 0.25,
            "mass_following": 0.15,
            "empty_bio": 0.10,
            "digit_heavy_username": 0.10,
        },
        rejected_rows=0,
        status=status,
    )


@pytest.mark.skipif(
    not Path("data/sample/verified_benchmarks.csv").is_file(),
    reason="Private local benchmark CSV is excluded from public clones",
)
def test_cli_end_to_end_generation(tmp_path: Path) -> None:
    """AC1: CLI execution generates the complete HTML report from real benchmark data."""
    output_html = tmp_path / "report.html"
    cmd = [
        sys.executable,
        "run_audit.py",
        "--input",
        "data/sample/verified_benchmarks.csv",
        "--output",
        str(output_html),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    assert result.returncode == 0
    assert output_html.is_file()
    assert output_html.stat().st_size > 1000


def test_cli_missing_input_file_prints_friendly_message() -> None:
    """AC1: Missing input file prints friendly message without traceback."""
    cmd = [
        sys.executable,
        "run_audit.py",
        "--input",
        "non_existent_path.csv",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "Error: Input file not found: non_existent_path.csv" in result.stderr
    assert "Traceback" not in result.stderr


def test_self_contained_no_external_links_in_src_or_href() -> None:
    """AC2: Self-contained report with no external links, scripts, or fonts in src/href."""
    artists = [_make_stat()]
    html_text = render_html_report(artists, 100, 0)

    # Chart must be embedded base64 data URI
    assert 'src="data:image/png;base64,' in html_text
    assert 'alt="Bar chart comparing estimated bot-like audience percentage across artists' in html_text

    # No external http://, https://, or protocol-relative // in src or href
    external_refs = re.findall(r'(?:src|href)\s*=\s*["\']((?:https?:)?//[^"\']+)["\']', html_text)
    assert external_refs == [], f"Found external references: {external_refs}"

    # No external CSS @import url(...) or background url(...)
    assert "@import" not in html_text
    assert "http://" not in html_text
    assert "https://" not in html_text


def test_contents_in_required_order() -> None:
    """AC3: Sections must appear in exact specified order."""
    artists = [_make_stat("BandAlpha")]
    html_text = render_html_report(artists, 100, 0)

    pos_title = html_text.find("Audience Quality Audit Report")
    pos_data_quality = html_text.find("Data Quality & Overview")
    pos_ranking = html_text.find("Audience Quality Ranking")
    pos_chart = html_text.find("Comparative Audience Visualization")
    pos_flag_breakdown = html_text.find("Warning Sign Breakdown")
    pos_glossary = html_text.find("Metric Explanations & Glossary")
    pos_limitations = html_text.find("Audit Limitations")

    assert pos_title != -1
    assert pos_data_quality != -1
    assert pos_ranking != -1
    assert pos_chart != -1
    assert pos_flag_breakdown != -1
    assert pos_glossary != -1
    assert pos_limitations != -1

    assert (
        pos_title
        < pos_data_quality
        < pos_ranking
        < pos_chart
        < pos_flag_breakdown
        < pos_glossary
        < pos_limitations
    )


def test_limitations_five_required_statements() -> None:
    """AC4: Limitations section must contain the 5 required statements."""
    artists = [_make_stat()]
    html_text = render_html_report(artists, 100, 0)
    lower = html_text.lower()

    # 1. Sample may not be random
    assert "sample may not be truly random" in lower
    # 2. Real people without posts or bios exist (quiet users)
    assert "quiet lurkers" in lower or "real quiet users exist" in lower
    # 3. Score describes audience quality, not whether artist is genuine
    assert "audience quality vs. artist authenticity" in lower
    # 4. Thresholds are rule-of-thumb heuristics
    assert "rule-of-thumb heuristics" in lower
    # 5. Cannot prove followers were bought
    assert "cannot prove follower purchases" in lower


def test_required_phrasing_and_zero_forbidden_words() -> None:
    """AC5: Must contain 'sample size' and 'bot-like audience', zero forbidden words."""
    artists = [_make_stat("ArtistTest")]
    html_text = render_html_report(artists, 100, 0)
    lower = html_text.lower()

    assert "sample size" in lower
    assert "bot-like audience" in lower

    for word in FORBIDDEN_WORDS:
        assert word not in lower, f"Forbidden word found: '{word}'"


def test_report_contains_zero_synthetic_banners_or_references() -> None:
    """Reports contain zero synthetic demo banners or computer-generated test notices."""
    artists = [_make_stat("Radiohead"), _make_stat("The_Beatles")]
    html_en = render_html_report(artists, 200, 0, lang="en")
    html_ro = render_html_report(artists, 200, 0, lang="ro")
    assert "Synthetic Test Data" not in html_en
    assert "banner-synthetic" not in html_en
    assert "Date Sintetice" not in html_ro
    assert "banner-synthetic" not in html_ro


@pytest.mark.skipif(
    not Path("data/sample/verified_benchmarks.csv").is_file(),
    reason="Private local benchmark CSV is excluded from public clones",
)
def test_no_usernames_present_in_report(tmp_path: Path) -> None:
    """AC7: The report lists NO individual follower usernames, only aggregate statistics."""
    sample_csv = Path("data/sample/verified_benchmarks.csv")
    assert sample_csv.is_file(), "verified_benchmarks.csv must exist for AC7 verification"

    # Extract all follower usernames from the sample CSV
    with sample_csv.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        usernames = {row["username"].strip() for row in reader if row.get("username")}

    assert len(usernames) >= 100, f"Expected at least 100 usernames, found {len(usernames)}"

    # Generate report via CLI pipeline
    report_file = tmp_path / "report.html"
    cmd = [
        sys.executable,
        "run_audit.py",
        "--input",
        str(sample_csv),
        "--output",
        str(report_file),
    ]
    subprocess.run(cmd, capture_output=True, text=True, check=True)
    report_content = report_file.read_text(encoding="utf-8")

    # Assert that zero follower usernames appear anywhere in the generated HTML
    leaked = [u for u in usernames if u in report_content]
    assert leaked == [], f"Found leaked follower usernames in report: {leaked[:5]}"


@pytest.mark.parametrize("forbidden_word", FORBIDDEN_WORDS)
def test_validate_report_content_raises_on_forbidden_words(forbidden_word: str) -> None:
    """AC5 negative test: validate_report_content raises ValueError on forbidden accusatory words."""
    bad_html = f"<html>sample size bot-like audience Warning: this is a {forbidden_word}!</html>"
    with pytest.raises(ValueError, match=f"Report contains forbidden accusatory term: '{forbidden_word}'"):
        validate_report_content(bad_html)


def test_validate_report_content_raises_on_missing_required_phrases() -> None:
    """AC5 negative test: validate_report_content raises ValueError when required phrases are missing."""
    # Missing 'sample size'
    with pytest.raises(ValueError, match="missing required phrase 'sample size'"):
        validate_report_content("<html>bot-like audience clean content</html>")

    # Missing 'bot-like audience'
    with pytest.raises(ValueError, match="missing required phrase 'bot-like audience'"):
        validate_report_content("<html>sample size clean content</html>")


def test_report_prose_reflects_dynamic_config() -> None:
    """Rule: Thresholds live only in config.yaml; glossary/limitations reflect active config."""
    from botaudit.config import load_config

    cfg = load_config()
    cfg["bot_like_cutoff"] = 0.55
    cfg["min_sample_size"] = 150
    cfg["mass_following_min_following"] = 2500
    cfg["digit_ratio_threshold"] = 0.35

    artists = [_make_stat("DynamicBand")]
    html_text = render_html_report(artists, 100, 0, config=cfg)

    assert "(0.55)" in html_text
    assert "above 0.55 exhibits" in html_text
    assert "(150 profiles)" in html_text
    assert "following over 2,500 accounts" in html_text
    assert "over 35% digits" in html_text


def test_report_rendering_with_zero_sample_size_artist() -> None:
    """Edge case: Report renders cleanly without error when an artist has zero valid sample size."""
    zero_stat = ArtistStats(
        artist="ZeroSampleArtist",
        sample_size=0,
        mean_bot_score=0.0,
        pct_bot_like=0.0,
        ci_low=0.0,
        ci_high=0.0,
        flag_rates={},
        rejected_rows=25,
        status="insufficient data",
    )
    html_text = render_html_report([zero_stat], 0, 25)
    assert "ZeroSampleArtist" in html_text
    assert "Insufficient Data" in html_text
    assert "N/A" in html_text


@pytest.mark.skipif(
    not Path("data/sample/verified_benchmarks.csv").is_file(),
    reason="Private local benchmark CSV is excluded from public clones",
)
def test_cli_nested_nonexistent_output_directory(tmp_path: Path) -> None:
    """Edge case: CLI creates nested output directories if they do not exist."""
    nested_output = tmp_path / "deep" / "nested" / "folder" / "audit_report.html"
    cmd = [
        sys.executable,
        "run_audit.py",
        "--input",
        "data/sample/verified_benchmarks.csv",
        "--output",
        str(nested_output),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    assert result.returncode == 0
    assert nested_output.is_file()


def test_cli_empty_csv_prints_friendly_error(tmp_path: Path) -> None:
    """Edge case: CLI prints clear CSV validation error on empty file without raw stack trace."""
    empty_csv = tmp_path / "empty.csv"
    empty_csv.write_text("", encoding="utf-8")
    cmd = [
        sys.executable,
        "run_audit.py",
        "--input",
        str(empty_csv),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "CSV Validation Error:" in result.stderr
    assert "Traceback" not in result.stderr


def test_html_escaping_prevents_xss_and_avoids_double_escaping() -> None:
    """AC8: HTML escaping is active; script tags are escaped as text, no double-escaping."""
    malicious_artist = "<script>alert(1)</script>"
    ampersand_artist = "Simon & Garfunkel"
    artists = [_make_stat(malicious_artist), _make_stat(ampersand_artist)]
    html_text = render_html_report(artists, 200, 0)

    # Malicious script tag escaped as text
    assert "<script>alert(1)</script>" not in html_text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html_text

    # Ampersand is cleanly escaped once in alt attribute, never double-escaped
    assert "Simon &amp; Garfunkel" in html_text
    assert "Simon &amp;amp; Garfunkel" not in html_text


def test_chart_and_full_report_handles_one_and_ten_artists() -> None:
    """AC9: Works with 1 artist and 10 artists with readable labels and complete rendering."""
    # 1 artist
    solo_artist = [_make_stat("SoloArtist")]
    base64_one, alt_one = generate_chart_base64(solo_artist)
    assert len(base64_one) > 500
    assert "SoloArtist" in alt_one
    html_one = render_html_report(solo_artist, 100, 0)
    assert "SoloArtist" in html_one

    # 10 artists
    ten_artists = [_make_stat(f"Artist_{i}") for i in range(1, 11)]
    base64_ten, alt_ten = generate_chart_base64(ten_artists)
    assert len(base64_ten) > 1000
    assert "Artist_1" in alt_ten
    assert "Artist_10" in alt_ten
    html_ten = render_html_report(ten_artists, 1000, 0)
    for i in range(1, 11):
        assert f"Artist_{i}" in html_ten


@pytest.mark.skipif(
    not Path("data/sample/verified_benchmarks.csv").is_file(),
    reason="Private local benchmark CSV is excluded from public clones",
)
def test_cli_custom_config_flag(tmp_path: Path) -> None:
    """AC1 extension: CLI accepts custom --config file and executes correctly."""
    output_html = tmp_path / "report_custom.html"
    cmd = [
        sys.executable,
        "run_audit.py",
        "--input",
        "data/sample/verified_benchmarks.csv",
        "--output",
        str(output_html),
        "--config",
        "config.yaml",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    assert result.returncode == 0
    assert output_html.is_file()
    assert output_html.stat().st_size > 1000


def test_validate_report_content_ignores_base64_image_bytes() -> None:
    """Base64 PNG bytes containing 'sCaM' or 'fRaUd' must not trigger false-positive matches."""
    html_with_b64 = (
        '<html><p>Sample size: 50, estimated bot-like audience: 10%</p>'
        '<img src="data:image/png;base64,iVBORw0KGgoAAAAsCaMfRaUd99==" alt="chart"></html>'
    )
    validate_report_content(html_with_b64)
