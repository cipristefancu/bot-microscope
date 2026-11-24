"""Username parsing and handle-pattern forensics (pure functions, no file or network access).

These helpers were moved out of web.py so label loading, capture rules and the
evaluation script can use them without importing the web stack.

This tool never connects to the internet.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

# Regex patterns for handle forensics
_WORD_DOT_DIGITS_RE = re.compile(r"^[a-z]+\.\d{5,}$", re.IGNORECASE)
_LONG_DIGIT_RUN_RE = re.compile(r"\d{5,}")
_ADJACENT_PUNCT_RE = re.compile(r"(\._|_\.|__|\.\.)")
_REPEATED_LETTER_RE = re.compile(r"([a-z])\1{3,}", re.IGNORECASE)
_CONSONANT_CLUSTER_RE = re.compile(r"^[bcdfghjklmnpqrstvwxz]{2,4}$", re.IGNORECASE)


def extract_username(raw_value: str) -> str:
    """Extract a clean Instagram handle from a profile URL or raw handle string.

    Examples:
        "https://www.instagram.com/zebra.9605848/" -> "zebra.9605848"
        "@musicfan_99" -> "musicfan_99"
        "  wired  " -> "wired"
    """
    text = raw_value.strip()
    if not text:
        return ""

    if "instagram.com/" in text.lower():
        parsed = urlparse(text if "://" in text else f"https://{text}")
        parts = [p for p in parsed.path.split("/") if p]
        if parts:
            # Handle paths like /p/..., /reel/..., or /<username>/
            candidate = parts[0].lstrip("@").strip()
            return candidate
        return ""

    return text.lstrip("@").strip("/").strip()


def compute_digit_ratio(username: str) -> float:
    """Calculate the share of numeric digits in a username (0.0 to 1.0)."""
    clean = username.strip()
    if not clean:
        return 0.0
    digits = sum(1 for ch in clean if ch.isdigit())
    return round(digits / len(clean), 4)


def is_stitched_or_glitch_handle(username: str) -> bool:
    """Detect bot-generated stitched punctuation, consonant-suffix, or repeated-char handles.

    Catches patterns such as:
        - '_.little_qf.y' (adjacent '_.', 4 punct chars, consonant cluster 'qf', 1-char 'y')
        - '_queen._s_7' (adjacent '._', 4 punct chars, 1-char 's' + digit '7')
        - 'zad._808', 'ale.__.20', 'c4.__l3', 'performaaaaa', 'whitenoizzzzz', 'alyy.nt_'
    """
    clean = username.strip().lower().lstrip("@")
    if not clean:
        return False
    if _ADJACENT_PUNCT_RE.search(clean):
        return True
    if _REPEATED_LETTER_RE.search(clean):
        return True
    punct_count = sum(1 for ch in clean if ch in "._")
    if punct_count >= 3:
        return True
    segments = [s for s in re.split(r"[._]+", clean) if s]
    if punct_count >= 2:
        short_segs = sum(1 for s in segments if len(s) <= 2)
        has_single = any(len(s) == 1 for s in segments)
        has_consonant_seg = any(bool(_CONSONANT_CLUSTER_RE.match(s)) for s in segments)
        if (has_single and short_segs >= 2) or has_consonant_seg:
            return True
    if (clean.startswith("_") or clean.endswith("_")) and punct_count >= 2:
        return True
    return bool(
        clean.endswith("_")
        and any(bool(_CONSONANT_CLUSTER_RE.match(s)) for s in segments if len(s) == 2)
    )


def analyze_handle_forensics(username: str, digit_threshold: float = 0.40) -> dict[str, Any]:
    """Evaluate structural naming patterns on an Instagram handle."""
    ratio = compute_digit_ratio(username)
    is_digit_heavy = ratio > digit_threshold
    is_word_dot_digits = bool(_WORD_DOT_DIGITS_RE.match(username))
    has_long_digit_run = bool(_LONG_DIGIT_RUN_RE.search(username))
    is_stitched = is_stitched_or_glitch_handle(username)
    return {
        "digit_ratio": ratio,
        "is_digit_heavy": is_digit_heavy,
        "is_word_dot_digits": is_word_dot_digits,
        "has_long_digit_run": has_long_digit_run,
        "is_stitched_handle": is_stitched,
    }
