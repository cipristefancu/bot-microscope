"""System-wide guard enforcing permanent elimination of all synthetic/fake demo data."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_BENCH_CSV = PROJECT_ROOT / "data" / "sample" / "verified_benchmarks.csv"


def test_synthetic_data_files_and_generators_do_not_exist() -> None:
    """Fake/synthetic data files and generator scripts must never exist on disk."""
    forbidden_paths = [
        PROJECT_ROOT / "make_sample_data.py",
        PROJECT_ROOT / "data" / "sample" / "sample_data.csv",
        PROJECT_ROOT / "data" / "calibration_combined.csv",
    ]
    existing = [str(p.relative_to(PROJECT_ROOT)) for p in forbidden_paths if p.exists()]
    assert existing == [], f"Forbidden synthetic files found: {existing}"


def test_template_csv_contains_only_header_zero_unmeasured_rows() -> None:
    """data/template.csv must be a clean 9-column header with zero unmeasured/fake rows."""
    tpl_path = PROJECT_ROOT / "data" / "template.csv"
    assert tpl_path.is_file()
    lines = [ln.strip() for ln in tpl_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    expected = (
        "artist,username,has_profile_photo,post_count,followers_count,"
        "following_count,bio_length,account_private,username_digit_ratio"
    )
    assert lines == [expected]


@pytest.mark.skipif(not _BENCH_CSV.is_file(), reason="Private local benchmark CSV is excluded from public clones")
def test_verified_benchmarks_csv_is_complete_and_non_synthetic() -> None:
    """data/sample/verified_benchmarks.csv must have >= 800 complete real observed rows."""
    bench_path = PROJECT_ROOT / "data" / "sample" / "verified_benchmarks.csv"
    assert bench_path.is_file()
    with bench_path.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) >= 800
    for row in rows:
        assert not row["artist"].upper().startswith("SYNTHETIC")
        for col, val in row.items():
            assert val is not None and val.strip() != "", f"Blank {col} in {row}"


def test_index_html_and_backend_have_zero_synthetic_demo_constructs() -> None:
    """Neither index.html nor src/botaudit/ may contain synthetic demo or fake-number helpers."""
    index_text = (PROJECT_ROOT / "index.html").read_text(encoding="utf-8")
    forbidden_index_tokens = [
        "SYNTHETIC DEMO",
        "Synthetic demo:",
        "Demo sintetic:",
        "EMBEDDED_BOTS_URLS",
        "EMBEDDED_CONTROL_URLS",
        "loadSampleHarJson",
        "loadTemplateExampleIntoCsvBox",
        "runClientSideFallback",
        "populateUrlsFromCalibration",
        "buildProfileDefaults",
        "runUrlsAudit",
        "runPreset('calibration'",
        'runPreset("calibration"',
    ]
    found_index = [tok for tok in forbidden_index_tokens if tok in index_text]
    assert found_index == [], f"Synthetic/fake constructs found in index.html: {found_index}"

    forbidden_backend_tokens = [
        "SYNTHETIC_DEMO_WARNING",
        "WHAT_IF_WARNING",
        "_build_calibration_csv",
        "urls_to_csv_text",
        "is_synthetic",
        "banner-synthetic",
    ]
    for path in sorted((PROJECT_ROOT / "src" / "botaudit").rglob("*")):
        if path.is_file() and path.suffix in {".py", ".j2", ".html"}:
            content = path.read_text(encoding="utf-8")
            hits = [tok for tok in forbidden_backend_tokens if tok in content]
            assert hits == [], f"Forbidden synthetic constructs in {path.name}: {hits}"
