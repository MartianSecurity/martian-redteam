"""Versioned scope readout: raw-logit decisions, separately calibrated probabilities.

The legacy probability-threshold functions in metrics.py remain for reproducing
historical experiments. New scope releases must use this explicit schema instead.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from .metrics import rates


def vector(values):
    result = np.asarray(values, dtype=np.float64)
    if result.ndim != 1 or not len(result) or not np.isfinite(result).all():
        raise ValueError("Expected a nonempty finite score vector")
    return result


def labeled(values, labels):
    values, labels = vector(values), vector(labels)
    if values.shape != labels.shape or set(labels) != {0, 1}:
        raise ValueError("Expected aligned scores and both binary labels")
    return values, labels


def sigmoid(values):
    values = vector(values)
    e = np.exp(-np.abs(values))
    return np.where(values >= 0, 1 / (1 + e), e / (1 + e))


class Calibration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    kind: Literal["regularized_affine_logit_v1"] = "regularized_affine_logit_v1"
    center: float
    scale: float = Field(gt=0)
    slope: float = Field(ge=0)
    intercept: float

    def logits(self, raw):
        return vector((vector(raw) - self.center) / self.scale * self.slope + self.intercept)

    def probabilities(self, raw):
        return sigmoid(self.logits(raw))


class OperatingPoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    schema_version: Literal[2] = 2
    score_space: Literal["raw_logit_float64"] = "raw_logit_float64"
    comparison: Literal["greater_than_or_equal"] = "greater_than_or_equal"
    threshold: float
    calibration: Calibration
    max_validation_false_block_rate: float = Field(ge=0, lt=1)
    enforcement_approved: Literal[False] = False

    def decisions(self, raw):
        return vector(raw) >= self.threshold


def calibrate(raw, labels):
    """Fit only calibration rows; fixed regularization prevents separable-data blowup.

    Projected Newton optimization of convex binary log loss. A zero slope is an
    explicit uninformative calibration, never a reversal of violation-score order.
    """
    raw, y = labeled(raw, labels)
    center, scale = float(raw.mean()), max(float(raw.std()), 1e-8)
    x = np.column_stack(((raw - center) / scale, np.ones(len(raw))))
    penalty = np.array([0.01, 0.0001], dtype=np.float64)
    weights = np.zeros(2, dtype=np.float64)

    def loss(w):
        z = x @ w
        return float(np.mean(np.logaddexp(0, z) - y * z) + (penalty * w * w).sum() / 2)

    for _ in range(100):
        p = sigmoid(x @ weights)
        gradient = x.T @ (p - y) / len(y) + penalty * weights
        hessian = (x.T * (p * (1 - p))) @ x / len(y) + np.diag(penalty)
        step = np.linalg.solve(hessian, gradient)
        previous = loss(weights)
        for power in range(40):
            candidate = weights - step * (0.5**power)
            candidate[0] = max(0, candidate[0])
            if loss(candidate) <= previous:
                break
        else:
            raise ValueError("Calibration optimization failed to descend")
        delta = float(np.max(np.abs(candidate - weights)))
        weights = candidate
        if delta < 1e-9:
            break
    else:
        raise ValueError("Calibration optimization did not converge")
    return Calibration(center=center, scale=scale, slope=float(weights[0]), intercept=float(weights[1]))


def budget_boundary(raw, labels, budget):
    raw, labels = labeled(raw, labels)
    if not np.isfinite(budget) or not 0 <= budget < 1:
        raise ValueError("Invalid false-block budget")
    benign = np.sort(raw[labels == 0])[::-1]
    permitted = int(np.floor(budget * len(benign)))
    return float(benign[permitted])


def threshold_after_boundary(raw, boundary):
    raw = vector(raw)
    above = raw[raw > boundary]
    # Select inside the observed gap rather than one ULP from a benign score,
    # preserving its validation decisions while reducing kernel-rounding sensitivity.
    minimum = np.nextafter(boundary, np.inf)
    threshold = float(max(minimum, boundary / 2 + float(above.min()) / 2)) if len(above) else float(minimum)
    if not np.isfinite(threshold):
        raise ValueError("No finite threshold exists")
    return threshold


def threshold_at_budget(raw, labels, budget):
    return threshold_after_boundary(raw, budget_boundary(raw, labels, budget))


def fit_operating_point(cal_raw, cal_labels, val_raw, val_labels, budget):
    return OperatingPoint(
        threshold=threshold_at_budget(val_raw, val_labels, budget),
        calibration=calibrate(cal_raw, cal_labels),
        max_validation_false_block_rate=budget,
    )


def fit_grouped_operating_point(cal_raw, cal_labels, val_raw, val_labels, budget, groups):
    val_raw, val_labels = labeled(val_raw, val_labels)
    boundaries = [budget_boundary(val_raw, val_labels, budget)]
    if not groups:
        raise ValueError("Grouped selection requires declared validation groups")
    for values in groups.values():
        values = np.asarray(values)
        if values.shape != val_raw.shape:
            raise ValueError("Validation groups must align with scores")
        for group in set(values):
            mask = values == group
            boundaries.append(budget_boundary(val_raw[mask], val_labels[mask], budget))
    return OperatingPoint(
        threshold=threshold_after_boundary(val_raw, max(boundaries)),
        calibration=calibrate(cal_raw, cal_labels),
        max_validation_false_block_rate=budget,
    )


def quality(raw, labels, operating_point):
    raw, labels = labeled(raw, labels)
    z = operating_point.calibration.logits(raw)
    probabilities = sigmoid(z)
    report = rates(raw, labels, operating_point.threshold)
    report.update(
        nll=float(np.mean(np.logaddexp(0, z) - labels * z)), brier=float(np.mean((probabilities - labels) ** 2))
    )
    bins, ece = [], 0.0
    for index in range(10):
        mask = (probabilities >= index / 10) & (probabilities < (index + 1) / 10 if index < 9 else probabilities <= 1)
        count = int(mask.sum())
        mean = float(probabilities[mask].mean()) if count else None
        prevalence = float(labels[mask].mean()) if count else None
        bins.append(
            dict(
                lower=index / 10,
                upper=(index + 1) / 10,
                count=count,
                mean_probability=mean,
                violation_fraction=prevalence,
            )
        )
        if count:
            ece += count / len(labels) * abs(mean - prevalence)
    report.update(reliability_bins=bins, ece_10_equal_width_bins=ece)
    return report
