"""HTML report generator with self-contained charts and honest statistical limits.

This module renders a single self-contained HTML audit report using Jinja2,
with an embedded base64 matplotlib chart. The report contains no external
dependencies (no external stylesheets, fonts, or scripts).

This tool never connects to the internet.
"""

from __future__ import annotations

import base64
import io
import re
from datetime import datetime
from pathlib import Path

import matplotlib
from jinja2 import Environment, FileSystemLoader

matplotlib.use("Agg")  # Non-interactive backend
import matplotlib.pyplot as plt

from botaudit.config import load_config
from botaudit.stats import ArtistStats

TEMPLATES_DIR = Path(__file__).parent / "templates"

# Forbidden terms: audited reports must evaluate audience quality objectively.
# Accusatory claims ("fake account", "scam") are scientifically unprovable from profile data.
FORBIDDEN_WORDS: tuple[str, ...] = (
    "fake account",
    "fake artist",
    "scam",
    "fraud",
)


_BASE64_DATA_URI_RE = re.compile(r"data:image/[^;]+;base64,[A-Za-z0-9+/=\s]+", re.IGNORECASE)


def validate_report_content(report_html: str) -> None:
    """Validate that required phrasing is present and forbidden terms are absent.

    Strips embedded base64 PNG data URIs before scanning prose so random binary
    byte sequences (e.g. '...sCaM...') never trigger false-positive word matches.

    Raises:
        ValueError: If required terms are missing or forbidden words are found.
    """
    prose_html = _BASE64_DATA_URI_RE.sub("data:image/png;base64,", report_html)
    lower = prose_html.lower()

    # Required phrasing (AC5)
    if "sample size" not in lower:
        raise ValueError("Report is missing required phrase 'sample size'.")
    if "bot-like audience" not in lower:
        raise ValueError("Report is missing required phrase 'bot-like audience'.")

    # Forbidden words (AC5)
    for word in FORBIDDEN_WORDS:
        if word in lower:
            raise ValueError(f"Report contains forbidden accusatory term: '{word}'.")


def generate_chart_base64(artists: list[ArtistStats], lang: str = "en") -> tuple[str, str]:
    """Generate a horizontal bar chart comparing artists with Wilson error bars.

    Returns:
        Tuple of (base64_png_string, alt_text).
    """
    is_ro = lang.lower() == "ro"
    valid_artists = [a for a in artists if a.sample_size > 0]
    if not valid_artists:
        # Generate placeholder chart if no valid data
        fig, ax = plt.subplots(figsize=(6, 2))
        empty_msg = (
            "Date insuficiente pentru vizualizare"
            if is_ro
            else "Insufficient data for visualization"
        )
        ax.text(0.5, 0.5, empty_msg, ha="center", va="center")
        ax.axis("off")
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight")
        plt.close(fig)
        buf.seek(0)
        alt_empty = (
            "Nu există date disponibile pentru grafic."
            if is_ro
            else "No data available for chart."
        )
        return (base64.b64encode(buf.read()).decode("utf-8"), alt_empty)

    # Sort bottom-to-top so highest ranked artist appears at top of horizontal chart
    chart_artists = list(reversed(valid_artists))
    names = [a.artist for a in chart_artists]
    values = [a.pct_bot_like * 100 for a in chart_artists]

    # Calculate asymmetric error bars from Wilson CI:
    # lower_err = value - ci_low
    # upper_err = ci_high - value
    err_low = [max(0.0, val - (a.ci_low * 100)) for a, val in zip(chart_artists, values)]
    err_high = [max(0.0, (a.ci_high * 100) - val) for a, val in zip(chart_artists, values)]
    xerr = [err_low, err_high]

    # Dynamically scale chart height based on artist count (AC9: readable for 1 to 10+ artists)
    fig_height = max(3.5, len(chart_artists) * 0.55 + 1.2)
    fig, ax = plt.subplots(figsize=(8.5, fig_height), dpi=130)

    y_pos = range(len(chart_artists))
    bars = ax.barh(
        y_pos,
        values,
        xerr=xerr,
        capsize=4,
        color="#3b82f6",
        edgecolor="#1d4ed8",
        alpha=0.85,
        height=0.6,
        error_kw={"elinewidth": 1.4, "ecolor": "#0f172a"},
    )

    ax.set_yticks(list(y_pos))
    ax.set_yticklabels(names, fontsize=9.5, fontweight="normal")
    xlabel = (
        "% Estimat de Audiență Tip Bot (cu Interval Wilson 95%)"
        if is_ro
        else "Estimated Bot-Like Audience % (with 95% Wilson CI)"
    )
    ax.set_xlabel(xlabel, fontsize=9.5, fontweight="normal", labelpad=8)
    ax.set_xlim(0, 100)
    ax.grid(axis="x", linestyle="--", alpha=0.5, color="#cbd5e1")
    ax.set_axisbelow(True)

    # Annotate percentage next to bars
    for bar, val in zip(bars, values):
        ax.text(
            val + 2.0,
            bar.get_y() + bar.get_height() / 2.0,
            f"{val:.1f}%",
            va="center",
            ha="left",
            fontsize=8.5,
            color="#334155",
            fontweight="bold",
        )

    # Styling clean border
    for spine in ["top", "right"]:
        ax.spines[spine].set_visible(False)
    for spine in ["left", "bottom"]:
        ax.spines[spine].set_color("#94a3b8")

    plt.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=130)
    plt.close(fig)
    buf.seek(0)
    base64_str = base64.b64encode(buf.read()).decode("utf-8")

    summary_entries = [f"{a.artist}: {a.pct_bot_like*100:.1f}%" for a in valid_artists]
    if is_ro:
        alt_text = (
            f"Grafic comparativ cu procentul estimat de audiență tip bot: "
            f"{', '.join(summary_entries)}."
        )
    else:
        alt_text = (
            f"Bar chart comparing estimated bot-like audience percentage across artists: "
            f"{', '.join(summary_entries)}."
        )

    return (base64_str, alt_text)


def render_html_report(
    artists: list[ArtistStats],
    total_rows_loaded: int,
    total_rows_rejected: int,
    warnings: list[str] | None = None,
    generated_date: str | None = None,
    config: dict | None = None,
    lang: str = "en",
) -> str:
    """Render a fully self-contained HTML audit report in English ('en') or Romanian ('ro').

    Args:
        artists: List of ArtistStats for all analyzed artists.
        total_rows_loaded: Total number of valid rows loaded.
        total_rows_rejected: Total number of rejected rows.
        warnings: List of data quality warnings.
        generated_date: ISO or readable timestamp string. Defaults to current time.
        config: Configuration dictionary. If None, default config is loaded.
        lang: Language code ('en' or 'ro'). Defaults to 'en'.

    Returns:
        Complete self-contained HTML document string.
    """
    if generated_date is None:
        generated_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if config is None:
        config = load_config()

    # Generate embedded chart (AC2, AC9)
    chart_base64, chart_alt_text = generate_chart_base64(artists, lang=lang)

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=True,
    )
    template_name = "report_ro.html.j2" if lang.lower() == "ro" else "report.html.j2"
    template = env.get_template(template_name)

    report_html = template.render(
        artists=artists,
        total_rows_loaded=total_rows_loaded,
        total_rows_rejected=total_rows_rejected,
        warnings=warnings or [],
        generated_date=generated_date,
        chart_base64=chart_base64,
        chart_alt_text=chart_alt_text,
        config=config,
    )

    # Validate against forbidden words and required phrases
    validate_report_content(report_html)

    return report_html


def write_html_report(
    destination: Path | str,
    artists: list[ArtistStats],
    total_rows_loaded: int,
    total_rows_rejected: int,
    warnings: list[str] | None = None,
    generated_date: str | None = None,
    config: dict | None = None,
    lang: str = "en",
) -> Path:
    """Render and write the self-contained HTML audit report to a file."""
    dest = Path(destination)
    dest.parent.mkdir(parents=True, exist_ok=True)

    report_html = render_html_report(
        artists=artists,
        total_rows_loaded=total_rows_loaded,
        total_rows_rejected=total_rows_rejected,
        warnings=warnings,
        generated_date=generated_date,
        config=config,
        lang=lang,
    )

    dest.write_text(report_html, encoding="utf-8")
    return dest
