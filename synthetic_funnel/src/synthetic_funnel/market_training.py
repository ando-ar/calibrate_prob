"""In-market warmup training from observed referral history only.

The models here are fitted on the referrals simulated before ``warmup_end`` and
the outcomes actually observed for them.  No latent counterfactual value,
market probability or post-warmup outcome is used as a feature or a label.
Drawing for downsampling comes from a dedicated RNG so neither the market nor
the counterfactual stream is advanced by training.
"""

from datetime import timedelta

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

from .generator import FEATURE_FIELDS, STAGES
from .market import INTRODUCTION_OFFSET_DAYS, TRAINING_STREAM, features
from .trained_scores import deadline, predict_scores

MODEL_SCORE_FIELDS = ('model_apply_score', 'model_screen_score', 'model_decision_score')
DATE_FIELDS = ('application', 'screening', 'decision')


def _deadline_days(start, referral_days):
    """Three-calendar-month horizons as offsets from ``start``, vectorized."""
    base = np.floor(referral_days).astype(np.int64)
    fraction = referral_days - base
    unique, inverse = np.unique(base, return_inverse=True)
    naive = start.replace(tzinfo=None)
    offsets = np.array([(deadline(naive + timedelta(days=int(day))) - naive).days for day in unique], dtype=float)
    return offsets[inverse] + fraction


def _cap_per_day(indices, days, rng, cap):
    """Keep at most ``cap`` training rows per introduction day."""
    values = days[indices]
    kept = []
    for day in np.unique(values):
        group = indices[values == day]
        if len(group) > cap:
            group = rng.choice(group, cap, replace=False)
        kept.append(group)
    return np.concatenate(kept) if kept else np.empty(0, dtype=np.int64)


def _stage_sample(stage, universe, x, reached, entry, days, rng, config, warmup_end, window):
    """Balanced, reservoir-capped training sample for one stage, or None."""
    risk = np.ones(len(x), dtype=bool) if stage == 0 else reached[:, stage - 1]
    mask = universe & risk & (entry >= warmup_end - window) & (entry < warmup_end)
    indices = _cap_per_day(np.flatnonzero(mask), days, rng, config.training_referrals_per_day)
    positives = indices[reached[indices, stage]]
    negatives = indices[~reached[indices, stage]]
    if len(positives) > config.negative_reservoir_size:
        positives = rng.choice(positives, config.negative_reservoir_size, replace=False)
    if len(positives) < 2 or len(negatives) < len(positives):
        return None
    cap = config.negative_reservoir_size
    if len(negatives) > cap:
        negatives = rng.choice(negatives, cap, replace=False)
    chosen = rng.choice(negatives, len(positives), replace=False)
    selected = np.sort(np.concatenate((positives, chosen)))
    return selected, x[selected], reached[selected, stage]


def _fit_stage(x, y):
    scaler = StandardScaler().fit(x)
    model = LogisticRegression(C=1., max_iter=2000).fit(scaler.transform(x), y)
    if model.n_iter_.max() >= 2000:
        raise ValueError('Warmup model did not converge')
    return model, scaler


def _windows(config, stage):
    """Widen a short window until the observed market history can support it."""
    window = config.training_window_days[stage]
    while True:
        yield window
        if window >= config.warmup:
            return
        window = min(config.warmup, window * 2)


def train_warmup_models(rows, seekers, jobs, config, start):
    """Fit the three balanced models on warmup referrals.

    Returns ``(bundle, info)``; ``bundle`` is None when no model can be
    trained (no warmup, or too few observed outcomes).  The info dictionary is
    the manifest ``training`` entry and always records the requested settings.
    """
    warmup_end = float(config.warmup)
    info = {
        'method': 'balanced_logistic_regression',
        'warmup': config.warmup,
        'warmup_end': (start + timedelta(days=warmup_end)).isoformat(),
        'training_cutoff': None,
        'training_window_days': list(config.training_window_days),
        'training_referrals_per_day': config.training_referrals_per_day,
        'negative_reservoir_size': config.negative_reservoir_size,
        'seed': config.seed,
        'label': 'common 3 calendar months from referral; phase-specific reached risk sets',
        'sampling_correction': False,
        'class_weights': 'none',
        'status': 'not_trained',
    }
    if config.warmup < 1 or config.warmup >= config.days or not len(rows):
        return None, info

    days = rows['day'].astype(np.int64)
    introduction = days.astype(float) + INTRODUCTION_OFFSET_DAYS
    warmup = days < config.warmup
    if not warmup.any():
        return None, info
    horizon = _deadline_days(start, introduction)
    observed = features(rows, seekers, jobs)
    x = np.column_stack([np.asarray(observed[field], dtype=float) for field in FEATURE_FIELDS])
    reached = np.column_stack([np.isfinite(rows[field]) & (rows[field] <= horizon) for field in DATE_FIELDS])
    entry = (introduction, rows['application'], rows['screening'])
    rng = np.random.default_rng(np.random.SeedSequence([config.seed, TRAINING_STREAM]))

    def plan(universe):
        stages = []
        for stage in range(len(STAGES)):
            for window in _windows(config, stage):
                sample = _stage_sample(stage, universe, x, reached, entry[stage], days, rng,
                                       config, warmup_end, window)
                if sample is not None:
                    stages.append((sample, window))
                    break
            else:
                return None
        return stages

    # Prefer labels whose full three-month horizon falls inside the warmup.
    # A short warmup cannot contain that horizon, so fall back to outcomes
    # observed before warmup_end (still never using latent or future values).
    mature = warmup & (horizon <= warmup_end)
    stages = plan(mature) if mature.any() else None
    universe = mature if stages is not None else warmup
    if stages is None:
        stages = plan(warmup)
    if stages is None:
        return None, info

    models = []
    summaries = []
    for stage, ((selected, sample_x, sample_y), window) in enumerate(stages):
        model, scaler = _fit_stage(sample_x, sample_y)
        models.append({
            'features': list(FEATURE_FIELDS), 'mean': scaler.mean_.tolist(), 'scale': scaler.scale_.tolist(),
            'coefficients': model.coef_[0].tolist(), 'intercept': float(model.intercept_[0]),
        })
        summaries.append({
            'stage': STAGES[stage], 'window_days': window, 'training_rows': len(selected),
            'positives': int(sample_y.sum()), 'negatives': int((~sample_y).astype(bool).sum()),
        })
    info.update(status='trained', training_cutoff=(start + timedelta(days=float(introduction[universe].max()))).isoformat(),
                stages=summaries)
    bundle = {'method': 'balanced_logistic_regression', 'models': models, **info}
    return bundle, info


def score_warmup_referrals(rows, seekers, jobs, config, start):
    """Return ``(scores, info)`` for in-market scoring.

    ``scores`` has one row per referral and one column per stage.  Referrals
    introduced before ``warmup_end`` (and every referral when no model could be
    trained) get NaN, which the exporters publish as NULL.
    """
    scores = np.full((len(rows), len(MODEL_SCORE_FIELDS)), np.nan)
    bundle, info = train_warmup_models(rows, seekers, jobs, config, start)
    if bundle is None:
        return scores, info
    scored = rows['day'] >= config.warmup
    if scored.any():
        observed = features(rows[scored], seekers, jobs)
        events = [dict(zip(FEATURE_FIELDS, values, strict=True)) for values in zip(
            *(np.asarray(observed[field], dtype=float).tolist() for field in FEATURE_FIELDS), strict=True,
        )]
        try:
            with np.errstate(over='raise', invalid='raise', divide='raise'):
                scores[scored] = predict_scores(events, bundle)
        except FloatingPointError as exc:
            raise ValueError('Warmup models produced invalid market scores') from exc
        if not np.isfinite(scores[scored]).all():
            raise ValueError('Warmup models produced invalid market scores')
    return scores, info
