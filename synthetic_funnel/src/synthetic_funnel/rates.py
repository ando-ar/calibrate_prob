"""Anchor expected conditional transition rates on a fixed reference population."""
import random
from dataclasses import dataclass, replace
from functools import lru_cache

import numpy as np

from .mechanism import (
    array_sigmoid,
    bound_stage_probability,
    configured_gap_penalties,
    score_components,
)
from .population import draw_pair, make_jobs


@dataclass(frozen=True)
class RateAnchor:
    intercepts: tuple
    expected_rates: tuple
    reference_rows: int
    reference_seed: int


def population_rates(config):
    # Outputs of an identical population use the same anchors regardless of
    # output row count/date range. No realized labels or future evaluation rows.
    canonical = replace(config, rows=1, referral_start='2000-01-01T00:00:00+09:00',
                        referral_end='2000-01-01T00:00:00+09:00', as_of='2000-01-01T00:00:00+09:00')
    return _population_rates(canonical)


@lru_cache(maxsize=32)
def _population_rates(config):
    rng = random.Random(config.reference_seed)
    jobs = make_jobs(config)
    rows, candidate, employer = [], [], []
    for _ in range(config.reference_rows):
        f, a, b = draw_pair(config, rng, jobs[rng.randrange(len(jobs))])
        rows.append(f)
        candidate.append(a)
        employer.append(b)
    features = {key: np.array([row[key] for row in rows]) for key in rows[0]}
    scores = score_components(config, features, np.array(candidate), np.array(employer))
    weights = np.ones(config.reference_rows)
    stage_p, intercepts, rates = [], [], []
    for i, (target, (c, e)) in enumerate(zip(config.base_probabilities, scores)):
        if i == 2:
            withdrawal, ng = configured_gap_penalties(stage_p[0], stage_p[1], config)
            c, e = c - withdrawal, e - ng
        intercept, p, rate = _anchor_stage(config, i, target, c, e, weights)
        if abs(rate - target) > 1e-9:
            raise ValueError('Target rate convergence failure')
        intercepts.append(intercept)
        rates.append(rate)
        stage_p.append(p)
        weights *= p
    return RateAnchor(tuple(intercepts), tuple(rates), config.reference_rows, config.reference_seed)


def _anchor_stage(config, stage, target, candidate_logits, employer_logits, weights):
    total_weight = np.sum(weights)

    def probabilities(intercept):
        candidate = array_sigmoid(candidate_logits + intercept)
        raw = candidate if stage == 0 else candidate * array_sigmoid(employer_logits + intercept)
        return bound_stage_probability(raw, config, stage)

    def mean(intercept):
        return float(np.sum(weights * probabilities(intercept)) / total_weight)

    low, high = -1.0, 1.0
    for _ in range(60):
        if mean(low) <= target <= mean(high):
            break
        low *= 2
        high *= 2
    else:
        raise ValueError('Unable to bracket target rate; check extreme settings')
    for _ in range(55):
        mid = (low + high) / 2
        if mean(mid) < target:
            low = mid
        else:
            high = mid
    intercept = (low + high) / 2
    probabilities_at_anchor = probabilities(intercept)
    rate = float(np.sum(weights * probabilities_at_anchor) / total_weight)
    return intercept, probabilities_at_anchor, rate
