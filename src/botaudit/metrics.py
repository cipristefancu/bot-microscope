"""Classifier metrics reported with 95% Wilson confidence intervals.

"95%" in this project means a 95% Wilson confidence interval. The interval comes from
the single implementation in stats.compute_wilson_ci and is reused here, not rewritten.
Bot-like is the "positive" class everywhere.

This tool never connects to the internet.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from botaudit.stats import compute_wilson_ci


@dataclass(frozen=True)
class Proportion:
    """successes out of total, with its Wilson confidence interval."""

    successes: int
    total: int
    low: float
    high: float

    @property
    def value(self) -> float | None:
        return None if self.total == 0 else self.successes / self.total

    def format(self) -> str:
        if self.value is None:
            return "n/a (0/0)"
        return (
            f"{100 * self.value:.1f}% [{100 * self.low:.1f}, {100 * self.high:.1f}] "
            f"({self.successes}/{self.total})"
        )


def proportion(successes: int, total: int, confidence: float = 0.95) -> Proportion:
    """A proportion with its Wilson interval at the given confidence level."""
    low, high = compute_wilson_ci(successes, total, confidence_level=confidence)
    return Proportion(successes, total, low, high)


@dataclass(frozen=True)
class Confusion:
    """Counts of correct and wrong calls against the owner's labels."""

    tp: int  # bot called bot-like
    fp: int  # human called bot-like
    tn: int  # human called human-like
    fn: int  # bot called human-like

    @property
    def positives(self) -> int:
        return self.tp + self.fn

    @property
    def negatives(self) -> int:
        return self.tn + self.fp

    def sensitivity(self, confidence: float = 0.95) -> Proportion:
        """Share of real bots that were caught."""
        return proportion(self.tp, self.positives, confidence)

    def specificity(self, confidence: float = 0.95) -> Proportion:
        """Share of real people that were left alone."""
        return proportion(self.tn, self.negatives, confidence)

    def ppv(self, confidence: float = 0.95) -> Proportion:
        """Of everything called bot-like, the share that really is a bot."""
        return proportion(self.tp, self.tp + self.fp, confidence)

    @property
    def youden_j(self) -> float | None:
        """Sensitivity + specificity - 1: 0 is a coin flip, 1 is perfect."""
        if self.positives == 0 or self.negatives == 0:
            return None
        return self.tp / self.positives + self.tn / self.negatives - 1.0

    @property
    def balanced_accuracy(self) -> float | None:
        j = self.youden_j
        return None if j is None else (j + 1.0) / 2.0


def confusion(truth: Iterable[bool], predicted: Iterable[bool]) -> Confusion:
    """Count outcomes; `truth` and `predicted` are True for bot-like."""
    tp = fp = tn = fn = 0
    for actual, called in zip(truth, predicted, strict=True):
        if actual and called:
            tp += 1
        elif actual:
            fn += 1
        elif called:
            fp += 1
        else:
            tn += 1
    return Confusion(tp, fp, tn, fn)


def error_amplification(youden_j: float | None) -> float | None:
    """How much a classifier's errors inflate the error of an audience-level percentage.

    The share estimate (observed + specificity - 1) / (sensitivity + specificity - 1)
    divides by Youden's J, so errors grow by 1 / J. None when J <= 0 (unusable).
    """
    if youden_j is None or youden_j <= 0:
        return None
    return 1.0 / youden_j
