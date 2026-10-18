"""Declared calibration and operating-point measurements for the research pipeline."""

from __future__ import annotations

import numpy as np


def probability(logits: np.ndarray, temperature: float) -> np.ndarray:
    return 1 / (1 + np.exp(-np.clip(logits / temperature, -40, 40)))


def calibrate(logits: np.ndarray, labels: np.ndarray) -> float:
    # A fixed grid avoids tuning optimization settings on validation/final data.
    temperatures = np.geomspace(0.05, 20, 241)
    losses = [np.mean(np.logaddexp(0, logits / t) - labels * logits / t) for t in temperatures]
    return float(temperatures[int(np.argmin(losses))])


def threshold_at_budget(scores: np.ndarray, labels: np.ndarray, budget: float) -> float:
    if not np.any(labels == 0) or not np.any(labels == 1):
        raise ValueError("Threshold selection requires allowed and violating decisions")
    # Score >= threshold means predicted violation. Above-one explicitly allows a
    # reject-none experimental operating point when every useful threshold fails.
    candidates = np.r_[0.0, np.unique(scores), np.nextafter(1.0, 2.0)]
    feasible = [float(t) for t in candidates if np.mean(scores[labels == 0] >= t) <= budget]
    return min(feasible)


def rates(scores, labels, threshold):
    predicted = scores >= threshold
    negatives, positives = labels == 0, labels == 1
    return {
        "count": int(len(labels)),
        "allowed": int(negatives.sum()),
        "violations": int(positives.sum()),
        "false_blocks": int(np.sum(predicted & negatives)),
        "missed_violations": int(np.sum(~predicted & positives)),
        "false_block_rate": float(np.mean(predicted[negatives])) if negatives.any() else None,
        "violation_recall": float(np.mean(predicted[positives])) if positives.any() else None,
    }


def quality(logits, labels, families, *, temperature, threshold):
    scores = probability(logits, temperature)
    result = rates(scores, labels, threshold)
    result.update(
        {
            "nll": float(np.mean(np.logaddexp(0, logits / temperature) - labels * logits / temperature)),
            "brier": float(np.mean((scores - labels) ** 2)),
            "reliability_bins": [],
        }
    )
    ece = 0.0
    for i in range(10):
        mask = (scores >= i / 10) & ((scores < (i + 1) / 10) if i < 9 else (scores <= 1))
        n = int(mask.sum())
        confidence = float(scores[mask].mean()) if n else None
        prevalence = float(labels[mask].mean()) if n else None
        result["reliability_bins"].append(
            {
                "lower": i / 10,
                "upper": (i + 1) / 10,
                "count": n,
                "mean_probability": confidence,
                "violation_fraction": prevalence,
            }
        )
        if n:
            ece += n / len(labels) * abs(confidence - prevalence)
    result["ece_10_equal_width_bins"] = ece
    groups = sorted(set(families))
    rng = np.random.default_rng(1701)
    bootstrap = []
    for _ in range(1000):
        selected = rng.choice(groups, len(groups), replace=True)
        indexes = np.concatenate([np.flatnonzero(families == family) for family in selected])
        bootstrap.append(rates(scores[indexes], labels[indexes], threshold))
    result["family_cluster_bootstrap_95_percentile"] = {
        key: np.percentile([b[key] for b in bootstrap], [2.5, 97.5]).tolist()
        for key in ("false_block_rate", "violation_recall")
    }
    result["independent_authored_families"] = len(groups)
    result["uncertainty_warning"] = (
        "Few authored families; intervals do not establish production coverage or a 1% risk bound"
    )
    return result
