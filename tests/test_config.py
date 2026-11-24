"""Tests for config.yaml and load_config().

Each test writes a slightly broken copy of config.yaml into a temporary
folder and checks that load_config() stops with a readable message.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from botaudit.config import (
    COUNT_KEYS,
    DEFAULT_CONFIG_PATH,
    FRACTION_KEYS,
    TOP_LEVEL_KEYS,
    WEIGHT_KEYS,
    ConfigError,
    load_config,
)


def _real_settings() -> dict[str, Any]:
    """The settings from the real config.yaml, as a fresh dict."""
    return yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))


def _write(tmp_path: Path, settings: Any) -> Path:
    """Write `settings` to a temporary config file and return its path."""
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(settings), encoding="utf-8")
    return path


def _changed(**changes: Any) -> dict[str, Any]:
    """A copy of the real settings with some top-level values replaced."""
    settings = copy.deepcopy(_real_settings())
    settings.update(changes)
    return settings


def _with_weight(name: str, value: Any) -> dict[str, Any]:
    """A copy of the real settings with one weight replaced."""
    settings = copy.deepcopy(_real_settings())
    settings["weights"][name] = value
    return settings


# --------------------------------------------------------- the valid file


def test_real_config_loads_with_required_values() -> None:
    """config.yaml loads and holds exactly the values from MILESTONES.md M1 AC3."""
    config = load_config()
    assert config["weights"] == {
        "no_photo": 0.20,
        "zero_posts": 0.25,
        "mass_following": 0.25,
        "empty_bio": 0.10,
        "digit_heavy_username": 0.20,
    }
    assert config["mass_following_min_following"] == 1500
    assert config["mass_following_max_followers"] == 50
    assert config["digit_ratio_threshold"] == 0.40
    assert config["bot_like_cutoff"] == 0.60
    assert config["min_sample_size"] == 100
    assert config["max_reject_fraction"] == 0.20
    assert config["confidence_level"] == 0.95


def test_every_setting_has_a_comment() -> None:
    """Each setting line in config.yaml has a comment directly above it."""
    lines = DEFAULT_CONFIG_PATH.read_text(encoding="utf-8").splitlines()
    setting_lines = [i for i, line in enumerate(lines) if re.match(r"\s*\w+:", line)]
    expected_count = len(TOP_LEVEL_KEYS) + len(WEIGHT_KEYS)
    assert len(setting_lines) == expected_count
    for i in setting_lines:
        assert i > 0 and lines[i - 1].strip().startswith("#"), (
            f"config.yaml line {i + 1} ({lines[i].strip()!r}) has no comment above it"
        )


def test_load_config_accepts_explicit_path(tmp_path: Path) -> None:
    """A valid copy in another folder loads too."""
    assert load_config(_write(tmp_path, _real_settings())) == _real_settings()


def test_config_saved_by_notepad_with_bom_loads(tmp_path: Path) -> None:
    """Windows Notepad may add an invisible BOM marker; it still loads."""
    path = tmp_path / "config.yaml"
    path.write_text(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8-sig")
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert load_config(path) == _real_settings()


# --------------------------------------------------- missing settings


@pytest.mark.parametrize("key", TOP_LEVEL_KEYS)
def test_missing_setting_is_named(tmp_path: Path, key: str) -> None:
    """Removing any top-level setting gives an error naming it."""
    settings = _real_settings()
    del settings[key]
    with pytest.raises(ConfigError, match=f"Missing setting '{key}'"):
        load_config(_write(tmp_path, settings))


@pytest.mark.parametrize("key", WEIGHT_KEYS)
def test_missing_weight_is_named(tmp_path: Path, key: str) -> None:
    """Removing any weight gives an error naming it."""
    settings = _real_settings()
    del settings["weights"][key]
    with pytest.raises(ConfigError, match=f"Missing setting 'weights.{key}'"):
        load_config(_write(tmp_path, settings))


# ------------------------------------------- unknown settings (typos)


def test_misspelled_setting_is_reported_with_suggestion(tmp_path: Path) -> None:
    """A typo is reported as unknown, the right name is suggested."""
    settings = _real_settings()
    settings["bot_like_cutof"] = settings.pop("bot_like_cutoff")
    with pytest.raises(ConfigError) as err:
        load_config(_write(tmp_path, settings))
    message = str(err.value)
    assert "Unknown setting 'bot_like_cutof'" in message
    assert "Did you mean 'bot_like_cutoff'?" in message
    assert "Missing setting 'bot_like_cutoff'" in message


def test_misspelled_weight_is_reported(tmp_path: Path) -> None:
    """A typo inside the weights section is reported too."""
    settings = _real_settings()
    settings["weights"]["no_foto"] = settings["weights"].pop("no_photo")
    with pytest.raises(ConfigError, match="Unknown setting 'weights.no_foto'"):
        load_config(_write(tmp_path, settings))


def test_extra_unknown_setting_is_reported(tmp_path: Path) -> None:
    """An extra setting nobody uses is reported (it may be a typo)."""
    with pytest.raises(ConfigError, match="Unknown setting 'surprise'"):
        load_config(_write(tmp_path, _changed(surprise=1)))


# -------------------------------------------------------- bad values


@pytest.mark.parametrize("key", WEIGHT_KEYS)
def test_negative_weight_is_rejected(tmp_path: Path, key: str) -> None:
    """A negative weight is rejected with its name."""
    with pytest.raises(ConfigError, match=f"'weights.{key}' must not be negative"):
        load_config(_write(tmp_path, _with_weight(key, -0.1)))


@pytest.mark.parametrize("key", WEIGHT_KEYS)
def test_weight_greater_than_one_is_rejected(tmp_path: Path, key: str) -> None:
    """A weight greater than 1 is rejected with its name."""
    with pytest.raises(ConfigError, match=f"'weights.{key}' must not be greater than 1"):
        load_config(_write(tmp_path, _with_weight(key, 1.5)))


def test_zero_weight_is_allowed(tmp_path: Path) -> None:
    """A weight of 0 is allowed: it switches that warning sign off."""
    config = load_config(_write(tmp_path, _with_weight("empty_bio", 0)))
    assert config["weights"]["empty_bio"] == 0


@pytest.mark.parametrize("value", ["high", True, None, [0.2]])
def test_non_number_weight_is_rejected(tmp_path: Path, value: Any) -> None:
    """Text, yes/no, empty or list values are not valid weights."""
    with pytest.raises(ConfigError, match="'weights.no_photo' must be a number"):
        load_config(_write(tmp_path, _with_weight("no_photo", value)))


@pytest.mark.parametrize("key", FRACTION_KEYS)
@pytest.mark.parametrize("value", [-0.01, 1.01, 60])
def test_cutoff_outside_0_to_1_is_rejected(tmp_path: Path, key: str, value: float) -> None:
    """Shares and cutoffs below 0 or above 1 are rejected with their name."""
    with pytest.raises(ConfigError, match=f"'{key}' must be between 0 and 1"):
        load_config(_write(tmp_path, _changed(**{key: value})))


@pytest.mark.parametrize("key", ["bot_like_cutoff", "digit_ratio_threshold"])
@pytest.mark.parametrize("value", [0, 1])
def test_cutoff_edges_0_and_1_are_allowed(tmp_path: Path, key: str, value: float) -> None:
    """Exactly 0 and exactly 1 are inside the allowed range."""
    assert load_config(_write(tmp_path, _changed(**{key: value})))[key] == value


@pytest.mark.parametrize("value", [0, 1])
def test_confidence_level_must_be_strictly_inside(tmp_path: Path, value: float) -> None:
    """0% or 100% confidence makes no sense mathematically."""
    with pytest.raises(ConfigError, match="'confidence_level' must be above 0 and below 1"):
        load_config(_write(tmp_path, _changed(confidence_level=value)))


@pytest.mark.parametrize("key", FRACTION_KEYS)
def test_text_cutoff_is_rejected(tmp_path: Path, key: str) -> None:
    """A cutoff written as text (for example '60%') is rejected."""
    with pytest.raises(ConfigError, match=f"'{key}' must be a number"):
        load_config(_write(tmp_path, _changed(**{key: "60%"})))


@pytest.mark.parametrize("key", COUNT_KEYS)
@pytest.mark.parametrize("value", [-1, 10.5, "many", True])
def test_bad_count_is_rejected(tmp_path: Path, key: str, value: Any) -> None:
    """Counts must be whole numbers of 0 or more."""
    with pytest.raises(ConfigError, match=f"'{key}' must be a whole number"):
        load_config(_write(tmp_path, _changed(**{key: value})))


# --------------------------------------------------- broken files


def test_missing_file_gives_clear_error(tmp_path: Path) -> None:
    """A wrong path says the file was not found."""
    with pytest.raises(ConfigError, match="Settings file not found"):
        load_config(tmp_path / "nope.yaml")


def test_invalid_yaml_gives_clear_error(tmp_path: Path) -> None:
    """Broken YAML syntax is reported as such."""
    path = tmp_path / "config.yaml"
    path.write_text("weights: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="is not valid YAML"):
        load_config(path)


@pytest.mark.parametrize("content", ["", "just some text\n", "- a\n- list\n"])
def test_file_without_named_settings_is_rejected(tmp_path: Path, content: str) -> None:
    """Empty files, plain text or lists are not a settings file."""
    path = tmp_path / "config.yaml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError, match="must contain named settings"):
        load_config(path)


def test_weights_must_be_a_section(tmp_path: Path) -> None:
    """'weights: 0.5' instead of a section of five weights is rejected."""
    with pytest.raises(ConfigError, match="'weights' must be a section"):
        load_config(_write(tmp_path, _changed(weights=0.5)))


def test_all_problems_are_listed_at_once(tmp_path: Path) -> None:
    """Several mistakes are reported together, one per line."""
    settings = _changed(bot_like_cutoff=2, min_sample_size=-5)
    settings["weights"]["zero_posts"] = -1
    with pytest.raises(ConfigError) as err:
        load_config(_write(tmp_path, settings))
    message = str(err.value)
    assert message.startswith("Problems in ")
    assert message.count("\n  - ") == 3
