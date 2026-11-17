"""Generate introduced pairs; never treat unintroduced candidates as negatives."""
import hashlib
import json
import math
import platform
from dataclasses import asdict, dataclass, field
from datetime import datetime

from .export import Table, dataset_staging, write_rows
from .legacy_scores import LegacyScores
from .mechanism import Mechanism, pair_features
from .population import Population
from .rates import population_rates

VERSION = "0.11.0"
STAGES = ("application", "screening", "decision")
DATE_COLUMNS = ("oubo_dt", "shorui_tsuuka_dt", "kettei_dt")
FEATURE_FIELDS = (*pair_features(0.5, 0.5, 600, 600, 0, 0), 'other_applications', 'competing_candidates')
SCORE_FIELDS = ('legacy_apply_score', 'legacy_screen_score', 'legacy_decision_score')
EVENT_FIELDS = ("event_id", "user_id", "item_id", *FEATURE_FIELDS, *SCORE_FIELDS, "shoukai_dt", *DATE_COLUMNS)
TRUTH_FIELDS = ("event_id", *(f"p_{s}_eventual" for s in STAGES),
                *(f"p_candidate_{s}" for s in STAGES), *(f"p_employer_{s}" for s in STAGES),
                "seeker_affinity", "employer_affinity", "failure_stage", "failure_reason",
                "application_screening_gap", "relative_application_screening_gap",
                *(f"latent_{c}" for c in DATE_COLUMNS))
EVENT_TABLE = Table("events", EVENT_FIELDS, "event_id")
TRUTH_TABLE = Table("truth", TRUTH_FIELDS, "event_id", (("event_id", "events", "event_id"),))


@dataclass(frozen=True)
class Delay:
    """Parameters of the underlying normal, conditioned on days >= 0."""
    mean_days: float
    std_days: float

    def __post_init__(self):
        if any(isinstance(x, bool) or not isinstance(x, (int, float)) or
               not math.isfinite(x) or x < 0 for x in (self.mean_days, self.std_days)):
            raise ValueError("Delay mean_days and std_days must be finite and nonnegative")

    def sample(self, rng):
        if self.std_days == 0:
            return float(self.mean_days)
        while True:
            days = rng.gauss(self.mean_days, self.std_days)
            if days >= 0:
                return days


def parse_datetime(value):
    result = datetime.fromisoformat(value)
    if result.utcoffset() is None:
        raise ValueError("Datetimes must include a UTC offset")
    return result


@dataclass(frozen=True)
class Config:
    rows: int = 1000000
    seed: int = 42
    referral_start: str = "2024-01-01T00:00:00+09:00"
    referral_end: str = "2025-12-31T00:00:00+09:00"
    as_of: str = "2026-02-01T00:00:00+09:00"
    application_delay: Delay = field(default_factory=lambda: Delay(3, 2))
    screening_delay: Delay = field(default_factory=lambda: Delay(5, 4))
    decision_delay: Delay = field(default_factory=lambda: Delay(30, 20))
    # Target conditional population rates.
    base_probabilities: tuple = (0.01, 0.10, 0.07)
    probability_half_widths: tuple = (0.005, 0.05, 0.035)
    skill_effects: tuple = (2.0, 1.5, 1.0)
    mechanism: Mechanism = field(default_factory=Mechanism)
    population: Population = field(default_factory=Population)
    reference_rows: int = 50000
    reference_seed: int = 90210
    legacy_scores: LegacyScores = field(default_factory=LegacyScores)

    def __post_init__(self):
        if not isinstance(self.legacy_scores, LegacyScores):
            raise TypeError('legacy_scores must be a LegacyScores instance')
        if not isinstance(self.population, Population):
            raise TypeError('population must be a Population instance')
        if type(self.reference_rows) is not int or self.reference_rows < 100:
            raise ValueError('reference_rows must be an integer >= 100')
        if type(self.reference_seed) is not int:
            raise ValueError('reference_seed must be an integer')
        if not isinstance(self.mechanism, Mechanism):
            raise TypeError("mechanism must be a Mechanism instance")
        if type(self.rows) is not int or self.rows < 1:
            raise ValueError("rows must be a positive integer")
        if type(self.seed) is not int:
            raise ValueError("seed must be an integer")
        start, end, cutoff = map(parse_datetime, (self.referral_start, self.referral_end, self.as_of))
        if not start <= end <= cutoff:
            raise ValueError("Require referral_start <= referral_end <= as_of")
        for delay in (self.application_delay, self.screening_delay, self.decision_delay):
            if not isinstance(delay, Delay):
                raise TypeError("Each delay must be a Delay instance")
        if len(self.base_probabilities) != 3 or any(
            isinstance(p, bool) or not isinstance(p, (int, float)) or not 0 < p < 1
            for p in self.base_probabilities
        ):
            raise ValueError("base_probabilities must contain three numbers strictly between 0 and 1")
        if len(self.skill_effects) != 3 or any(
            isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x)
            for x in self.skill_effects
        ):
            raise ValueError("skill_effects must contain three finite numbers")
        object.__setattr__(self, 'base_probabilities', tuple(self.base_probabilities))
        object.__setattr__(self, 'skill_effects', tuple(self.skill_effects))
        if len(self.probability_half_widths) != 3 or any(type(w) not in (int, float) or not math.isfinite(w) or w <= 0 for w in self.probability_half_widths):
            raise ValueError('probability_half_widths must contain three positive finite values')
        object.__setattr__(self, 'probability_half_widths', tuple(self.probability_half_widths))
        if any(not 0 < base - width < base + width < 1
               for base, width in zip(self.base_probabilities, self.probability_half_widths, strict=True)):
            raise ValueError('Probability ranges must lie strictly inside (0, 1)')

    @classmethod
    def from_dict(cls, values):
        values = dict(values)
        if 'legacy_scores' in values:
            values['legacy_scores'] = LegacyScores(**values['legacy_scores'])
        if 'mechanism' in values:
            values['mechanism'] = Mechanism(**values['mechanism'])
        if 'population' in values:
            values['population'] = Population(**values['population'])
        for name in ("application_delay", "screening_delay", "decision_delay"):
            if name in values:
                values[name] = Delay(**values[name])
        for name in ("base_probabilities", "skill_effects", "probability_half_widths"):
            if name in values:
                values[name] = tuple(values[name])
        return cls(**values)


def _generate_unscored(config):
    from .batch import generate_batch

    yield from generate_batch(config)


def generate(config):
    if config.legacy_scores.enabled:
        from .trained_scores import generate_scored

        yield from generate_scored(config, _generate_unscored)
    else:
        yield from _generate_unscored(config)


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_dataset(config, output, progress=None, *, output_format="sqlite"):
    """Publish a new SQLite, Parquet or CSV dataset and its manifest."""
    with dataset_staging(output, output_format) as staging:
        return _write_dataset(config, staging, progress, output_format)


def _write_dataset(config, output, progress, output_format):
    expected = [0., 0., 0.]
    realized = [0, 0, 0]

    def records():
        for row_number, (row, answer) in enumerate(generate(config), 1):
            joint = 1.
            for i, stage in enumerate(STAGES):
                joint *= answer[f'p_{stage}_eventual']
                expected[i] += joint
                realized[i] += bool(answer[f'latent_{DATE_COLUMNS[i]}'])
            yield row, answer
            if progress is not None and (row_number % 100000 == 0 or row_number == config.rows):
                progress(row_number)

    files = write_rows(output, output_format, (EVENT_TABLE, TRUTH_TABLE), records())
    manifest = {"generator": "synthetic-funnel", "version": VERSION,
                "output_format": output_format,
                "python": platform.python_version(), "config": json.loads(json.dumps(asdict(config))),
                "delay_distribution": "normal conditioned on nonnegative days; continuous durations",
                "population": f"introduced pairs only; one introduction per user; {config.population.job_count} fixed jobs",
                "probability_scope": "eventual next-stage probability conditional on prior success, observed features AND hidden pair affinities; not 3-month probability or observed-feature marginal probability",
                "feature_timing": "all observed features fixed at introduction",
                "mechanism": "two independent acceptance draws per reached stage conditional on pair state; application has employer probability 1; employer-first failure attribution",
                "files": {name: file_hash(output / name) for name in files}}
    manifest['rate_scope'] = 'eventual transitions conditional on previous-stage arrival; NOT within 3 months'
    manifest['legacy_scores'] = {'enabled': config.legacy_scores.enabled}
    if config.legacy_scores.enabled:
        from .trained_scores import fit_models
        model_path = output / 'legacy_models.json'
        model_path.write_text(json.dumps(fit_models(config), ensure_ascii=False, indent=2), encoding='utf-8')
        manifest['files']['legacy_models.json'] = file_hash(model_path)
        manifest['legacy_scores'].update({
            'method': 'balanced_model',
            'meaning': 'uncorrected predict_proba after positive:negative=1:1 sampling',
            'temporal_scope': 'independent history fully matured before referral_start',
            'inputs': 'observed introduction features only; no truth or outcome inputs at inference',
        })
    manifest['probability_bounds'] = [
        [base - width, base + width]
        for base, width in zip(config.base_probabilities, config.probability_half_widths, strict=True)
    ]
    manifest['competition_scope'] = 'independent count snapshots at referral; not a joint simulation of competing agents'
    manifest['decision_gap_scope'] = 'p_application/base_application minus p_screening/base_screening'
    manifest['rate_anchor'] = asdict(population_rates(config))
    manifest['generated_expected_rates'] = [expected[i] / (config.rows if i == 0 else expected[i-1]) for i in range(3)]
    manifest['generated_realized_rates'] = [realized[i] / (config.rows if i == 0 else realized[i-1])
                                          if (config.rows if i == 0 else realized[i-1]) else None for i in range(3)]
    manifest['generated_latent_stage_counts'] = realized
    manifest = json.loads(json.dumps(manifest))
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest
