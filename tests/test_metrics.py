"""Tests for classifier metrics reported with 95% Wilson confidence intervals."""

from __future__ import annotations

import pytest

from botaudit.metrics import Confusion, confusion, error_amplification, proportion
from botaudit.stats import compute_wilson_ci


def test_wilson_interval_matches_a_hand_calculation() -> None:
    # 3 of 5 at 95%: center = (0.6 + 1.9600^2/10) / (1 + 1.9600^2/5) = 0.5566,
    # margin = 1.9600/1.7683 * sqrt(0.6*0.4/5 + 1.9600^2/100) = 0.3258  ->  [0.2307, 0.8824]
    p = proportion(3, 5)
    assert p.low == pytest.approx(0.2307, abs=1e-3)
    assert p.high == pytest.approx(0.8824, abs=1e-3)
    assert p.value == pytest.approx(0.6)


def test_it_is_the_projects_single_wilson_implementation() -> None:
    p = proportion(17, 40, confidence=0.90)
    assert (p.low, p.high) == compute_wilson_ci(17, 40, confidence_level=0.90)


def test_formatting_shows_value_interval_and_counts() -> None:
    assert proportion(3, 5).format() == "60.0% [23.1, 88.2] (3/5)"
    assert proportion(0, 0).format() == "n/a (0/0)"
    assert proportion(0, 0).value is None


def test_zero_errors_still_leaves_an_honest_interval() -> None:
    p = proportion(95, 95)
    assert p.value == 1.0
    assert p.low == pytest.approx(0.9611, abs=1e-3)  # 95 / (95 + 1.96^2)
    assert p.high == 1.0


def test_confusion_counts_and_rates() -> None:
    truth = [True, True, True, True, False, False, False, False, False, False]
    called = [True, True, True, False, True, False, False, False, False, False]
    c = confusion(truth, called)
    assert c == Confusion(tp=3, fp=1, tn=5, fn=1)
    assert c.positives == 4
    assert c.negatives == 6
    assert c.sensitivity().value == pytest.approx(0.75)
    assert c.specificity().value == pytest.approx(5 / 6)
    assert c.ppv().value == pytest.approx(0.75)
    assert c.youden_j == pytest.approx(0.75 + 5 / 6 - 1)
    assert c.balanced_accuracy == pytest.approx((0.75 + 5 / 6) / 2)


def test_confusion_with_a_missing_class_has_no_youden_j() -> None:
    c = confusion([True, True], [True, False])
    assert c.negatives == 0
    assert c.youden_j is None
    assert c.balanced_accuracy is None
    assert c.specificity().format() == "n/a (0/0)"


def test_confusion_rejects_lists_of_different_length() -> None:
    with pytest.raises(ValueError, match="shorter|longer"):
        confusion([True, False], [True])


@pytest.mark.parametrize(
    ("youden_j", "expected"),
    [(0.5, 2.0), (1.0, 1.0), (0.236, 1 / 0.236), (0.0, None), (-0.2, None), (None, None)],
)
def test_error_amplification(youden_j: float | None, expected: float | None) -> None:
    result = error_amplification(youden_j)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)
