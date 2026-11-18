"""Shared settings and helpers for the continuous-market generator.

This module holds only the pieces of the former row-based generator that the
market path still needs.  It has no generation modes, datasets or model
training: those live in the market modules.
"""

import calendar
import hashlib
import math
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from .mechanism import Mechanism, pair_features
from .population import Population

VERSION = "0.12.0"
STAGES = ("application", "screening", "decision")
DATE_COLUMNS = ("oubo_dt", "shorui_tsuuka_dt", "kettei_dt")
FEATURE_FIELDS = (*pair_features(0.5, 0.5, 600, 600, 0, 0), "other_applications", "competing_candidates")
MODEL_SCORE_FIELDS = ("model_apply_score", "model_screen_score", "model_decision_score")

# Default probability settings of the shared reference population.
BASE_PROBABILITIES = (0.01, 0.10, 0.07)
PROBABILITY_HALF_WIDTHS = (0.005, 0.05, 0.035)
SKILL_EFFECTS = (2.0, 1.5, 1.0)
REFERENCE_ROWS = 50000
REFERENCE_SEED = 90210


def parse_datetime(value):
    result = datetime.fromisoformat(value)
    if result.utcoffset() is None:
        raise ValueError("Datetimes must include a UTC offset")
    return result


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def deadline(t):
    """Return the common three-calendar-month horizon after ``t``."""
    month = t.month + 3
    year = t.year + (month - 1) // 12
    month = (month - 1) % 12 + 1
    return t.replace(year=year, month=month, day=min(t.day, calendar.monthrange(year, month)[1]))


def predict_scores(rows, bundle):
    """Apply a balanced logistic-regression bundle to observed feature rows."""
    result = []
    for model in bundle["models"]:
        x = np.asarray([[row[k] for k in model["features"]] for row in rows], dtype=float)
        z = ((x - model["mean"]) / model["scale"]) @ np.asarray(model["coefficients"]) + model["intercept"]
        result.append(np.exp(-np.logaddexp(0, -z)))
    return np.column_stack(result)


@dataclass(frozen=True)
class ProbabilityConfig:
    """Probability/anchor settings shared by the market and its scoring.

    ``population_rates`` canonicalizes the date and row fields, so only the
    probability settings, the mechanism, the population and the anchor seeds
    affect the resulting intercepts.
    """

    seed: int = 42
    rows: int = 1
    referral_start: str = "2024-01-01T00:00:00+09:00"
    referral_end: str = "2025-12-31T00:00:00+09:00"
    as_of: str = "2026-02-01T00:00:00+09:00"
    base_probabilities: tuple = BASE_PROBABILITIES
    probability_half_widths: tuple = PROBABILITY_HALF_WIDTHS
    skill_effects: tuple = SKILL_EFFECTS
    mechanism: Mechanism = field(default_factory=Mechanism)
    population: Population = field(default_factory=Population)
    reference_rows: int = REFERENCE_ROWS
    reference_seed: int = REFERENCE_SEED

    def __post_init__(self):
        if type(self.seed) is not int or type(self.rows) is not int or self.rows < 1:
            raise ValueError("seed must be an integer and rows must be a positive integer")
        start, end, cutoff = map(parse_datetime, (self.referral_start, self.referral_end, self.as_of))
        if not start <= end <= cutoff:
            raise ValueError("Require referral_start <= referral_end <= as_of")
        if not isinstance(self.mechanism, Mechanism) or not isinstance(self.population, Population):
            raise TypeError("mechanism and population must be their respective dataclass instances")
        if type(self.reference_rows) is not int or self.reference_rows < 100:
            raise ValueError("reference_rows must be an integer >= 100")
        if type(self.reference_seed) is not int:
            raise ValueError("reference_seed must be an integer")
        if len(self.base_probabilities) != 3 or any(
            isinstance(p, bool) or not isinstance(p, (int, float)) or not 0 < p < 1
            for p in self.base_probabilities
        ):
            raise ValueError("base_probabilities must contain three numbers strictly between 0 and 1")
        object.__setattr__(self, "base_probabilities", tuple(self.base_probabilities))
        if len(self.skill_effects) != 3 or any(
            isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x)
            for x in self.skill_effects
        ):
            raise ValueError("skill_effects must contain three finite numbers")
        object.__setattr__(self, "skill_effects", tuple(self.skill_effects))
        if len(self.probability_half_widths) != 3 or any(
            type(w) not in (int, float) or not math.isfinite(w) or w <= 0 for w in self.probability_half_widths
        ):
            raise ValueError("probability_half_widths must contain three positive finite values")
        object.__setattr__(self, "probability_half_widths", tuple(self.probability_half_widths))
        if any(not 0 < base - width < base + width < 1 for base, width in
               zip(self.base_probabilities, self.probability_half_widths, strict=True)):
            raise ValueError("Probability ranges must lie strictly inside (0, 1)")

    @classmethod
    def from_dict(cls, values):
        values = dict(values)
        if "mechanism" in values:
            values["mechanism"] = Mechanism(**values["mechanism"])
        if "population" in values:
            values["population"] = Population(**values["population"])
        for name in ("base_probabilities", "probability_half_widths", "skill_effects"):
            if name in values:
                values[name] = tuple(values[name])
        return cls(**values)
