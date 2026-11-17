"""Assess probability calibration on held-out outcomes."""

from pathlib import Path

import numpy as np


def metrics(labels: np.ndarray, probabilities: np.ndarray, bins: int) -> dict:
    bucket = np.minimum((probabilities * bins).astype(int), bins - 1)
    counts = np.bincount(bucket, minlength=bins)
    sums = np.bincount(bucket, weights=probabilities, minlength=bins)
    positives = np.bincount(bucket, weights=labels, minlength=bins)
    groups = [
        {"lower": i / bins, "upper": (i + 1) / bins, "n": int(counts[i]),
         "mean_probability": float(sums[i] / counts[i]), "observed_rate": float(positives[i] / counts[i])}
        for i in np.flatnonzero(counts)
    ]
    return {
        "ece": float(np.abs(sums - positives).sum() / len(labels)),
        "brier_score": float(np.mean((probabilities - labels) ** 2)),
        "mean_probability": float(probabilities.mean()),
        "bins": groups,
    }


def write_plot(path: Path, calibrated: dict, before: dict | None, before_label: str, n: int):
    left, top, size = 80, 55, 340
    bottom = top + size
    elements = [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 620 465" role="img">',
        '<title>Calibration on held-out data</title>',
        '<rect width="620" height="465" fill="white"/>',
        '<g font-family="sans-serif" font-size="12" fill="#0f172a">',
        f'<text x="{left}" y="28" font-size="18">Calibration on held-out data (n={n:,})</text>',
    ]
    for i in range(6):
        rate = i / 5
        x, y = left + size * rate, bottom - size * rate
        elements.extend([
            f'<path d="M{x} {top}V{bottom} M{left} {y}H{left + size}" stroke="#e2e8f0"/>',
            f'<text x="{x}" y="{bottom + 20}" text-anchor="middle">{rate:.1f}</text>',
            f'<text x="{left - 12}" y="{y + 4}" text-anchor="end">{rate:.1f}</text>',
        ])
    elements.extend([
        f'<path d="M{left} {top}V{bottom}H{left + size}" fill="none" stroke="#334155"/>',
        f'<path d="M{left} {bottom}L{left + size} {top}" stroke="#64748b" stroke-dasharray="4 4"/>',
        f'<text x="{left + size / 2}" y="438" text-anchor="middle">Mean predicted probability</text>',
        '<text x="23" y="225" transform="rotate(-90 23 225)" text-anchor="middle">Observed positive rate</text>',
        '<text x="448" y="138" fill="#64748b">Perfect calibration</text>',
    ])
    for i, (label, scores, color) in enumerate((
        (before_label, before, "#a54b18"), ("After isotonic", calibrated, "#096899"),
    )):
        if scores is None:
            continue
        points = [(left + size * group["mean_probability"], bottom - size * group["observed_rate"])
                  for group in scores["bins"]]
        coordinates = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)
        elements.append(f'<polyline points="{coordinates}" fill="none" stroke="{color}" stroke-width="2"/>')
        for x, y in points:
            elements.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="4" fill="{color}"/>')
        elements.append(f'<text x="448" y="{162 + 24 * i}" fill="{color}">{label}</text>')
    elements.append("</g></svg>")
    path.write_text("\n".join(elements) + "\n", encoding="utf-8")
