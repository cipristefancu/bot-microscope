"""Read and check the settings file (config.yaml).

All thresholds and weights live in config.yaml. This module only knows the
NAMES of the settings and the rules they must follow; it contains no
default values, so the numbers can only come from config.yaml.
"""

from __future__ import annotations

import difflib
import math
from pathlib import Path
from typing import Any

import yaml

# Project root = two folders above this file (src/botaudit/config.py).
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.yaml"

# Names of the five warning-sign weights inside the "weights" section.
WEIGHT_KEYS: tuple[str, ...] = (
    "no_photo",
    "zero_posts",
    "mass_following",
    "empty_bio",
    "digit_heavy_username",
)

# Settings that must be whole numbers of 0 or more.
COUNT_KEYS: tuple[str, ...] = (
    "mass_following_min_following",
    "mass_following_max_followers",
    "min_sample_size",
)

# Settings that are shares or cutoffs and must be within 0 to 1.
FRACTION_KEYS: tuple[str, ...] = (
    "digit_ratio_threshold",
    "bot_like_cutoff",
    "max_reject_fraction",
    "confidence_level",
)

# Every top-level name allowed in config.yaml.
TOP_LEVEL_KEYS: tuple[str, ...] = ("weights", *COUNT_KEYS, *FRACTION_KEYS)


class ConfigError(ValueError):
    """Raised when config.yaml is missing, unreadable, or has bad values."""


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    """Load config.yaml, check every setting, and return them as a dict.

    Args:
        path: Location of the settings file. Defaults to config.yaml in
            the project folder.

    Returns:
        A dict with the same structure as config.yaml.

    Raises:
        ConfigError: with a plain-language list of every problem found.
    """
    data = _read_yaml(Path(path))
    problems = _check_names(data)
    if not problems:
        problems = _check_values(data)
    if problems:
        bullet_list = "\n".join(f"  - {p}" for p in problems)
        raise ConfigError(f"Problems in {path}:\n{bullet_list}")
    return data


def _read_yaml(path: Path) -> dict[str, Any]:
    """Read the file and make sure it holds named settings."""
    if not path.is_file():
        raise ConfigError(f"Settings file not found: {path}")
    try:
        # "utf-8-sig" also reads files that Notepad saved with a BOM marker.
        data = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except yaml.YAMLError as err:
        raise ConfigError(f"{path} is not valid YAML: {err}") from err
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain named settings, like 'bot_like_cutoff: 0.60'.")
    return data


def _check_names(data: dict[str, Any]) -> list[str]:
    """Find missing settings and unknown (probably misspelled) settings."""
    problems = _compare_keys(data, TOP_LEVEL_KEYS, where="")
    weights = data.get("weights")
    if "weights" in data and not isinstance(weights, dict):
        problems.append("'weights' must be a section containing the five weights.")
    elif isinstance(weights, dict):
        problems += _compare_keys(weights, WEIGHT_KEYS, where="weights.")
    return problems


def _compare_keys(found: dict[str, Any], expected: tuple[str, ...], where: str) -> list[str]:
    """List expected names that are missing and found names that are unknown."""
    problems = [f"Missing setting '{where}{key}'." for key in expected if key not in found]
    for key in found:
        if key not in expected:
            hint = difflib.get_close_matches(str(key), expected, n=1)
            tip = f" Did you mean '{where}{hint[0]}'?" if hint else ""
            problems.append(f"Unknown setting '{where}{key}' (typo?).{tip}")
    return problems


def _check_values(data: dict[str, Any]) -> list[str]:
    """Check that every value is a number in its allowed range."""
    problems: list[str] = []
    for key in WEIGHT_KEYS:
        value = data["weights"][key]
        if not _is_number(value):
            problems.append(f"'weights.{key}' must be a number, got {value!r}.")
        elif value < 0:
            problems.append(f"'weights.{key}' must not be negative, got {value}.")
        elif value > 1:
            problems.append(f"'weights.{key}' must not be greater than 1, got {value}.")
    for key in COUNT_KEYS:
        value = data[key]
        if not _is_number(value) or value != int(value) or value < 0:
            problems.append(f"'{key}' must be a whole number of 0 or more, got {value!r}.")
    for key in FRACTION_KEYS:
        value = data[key]
        if not _is_number(value):
            problems.append(f"'{key}' must be a number between 0 and 1, got {value!r}.")
        elif not 0 <= value <= 1:
            problems.append(f"'{key}' must be between 0 and 1, got {value}.")
    if _is_number(data["confidence_level"]) and data["confidence_level"] in (0, 1):
        problems.append("'confidence_level' must be above 0 and below 1, like 0.95.")
    return problems


def _is_number(value: Any) -> bool:
    """True for real numbers (int or float). Booleans and NaN do not count."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return not math.isnan(value) and not math.isinf(value)
