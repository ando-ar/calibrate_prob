"""Publish continuous market data in the shared referral dataset formats."""

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from .export import BATCH_SIZE, Table, dataset_staging, write_rows
from .generator import (
    DATE_COLUMNS,
    EVENT_TABLE,
    FEATURE_FIELDS,
    SCORE_FIELDS,
    STAGES,
    TRUTH_TABLE,
    VERSION,
    Config,
    file_hash,
    parse_datetime,
)
from .market import (
    FAILURE_REASONS,
    INTRODUCTION_OFFSET_DAYS,
    MarketConfig,
    features,
    simulate,
)

SEEKER_TABLE = Table(
    "seekers", ("user_id", "arrival_dt", "seeker_skill", "desired_salary", "occupation", "region",
                "wants_remote", "seeker_affinity"), "user_id",
)
JOB_TABLE = Table(
    "jobs", ("item_id", "arrival_dt", "required_skill", "offered_salary", "occupation", "region",
             "allows_remote", "employer_affinity"), "item_id",
)
DAILY_TABLE = Table("daily", ("day_dt", "live_seekers", "live_jobs", "referrals"), "day_dt")
MARKET_EVENT_TABLE = replace(EVENT_TABLE, foreign_keys=(
    ("user_id", SEEKER_TABLE.name, SEEKER_TABLE.key),
    ("item_id", JOB_TABLE.name, JOB_TABLE.key),
))


def _finite_number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _load_models(path: Path, start: datetime):
    source = path.read_bytes()
    bundle = json.loads(source)
    if not isinstance(bundle, dict) or bundle.get("method") != "balanced_logistic_regression":
        raise ValueError("legacy_models must be a synthetic-funnel balanced_logistic_regression bundle")
    value = bundle.get("training_cutoff")
    if not isinstance(value, str):
        raise TypeError("legacy_models training_cutoff must be an ISO 8601 datetime with UTC offset")
    try:
        cutoff = parse_datetime(value)
    except ValueError as exc:
        raise ValueError("legacy_models training_cutoff must be an ISO 8601 datetime with UTC offset") from exc
    if cutoff > start:
        raise ValueError("legacy_models training_cutoff must be at or before market_start")
    models = bundle.get("models")
    if not isinstance(models, list) or len(models) != len(STAGES):
        raise ValueError("legacy_models must contain exactly three models in application, screening, decision order")
    for stage, model in zip(STAGES, models, strict=True):
        names = model.get("features") if isinstance(model, dict) else None
        if (not isinstance(names, list) or not all(isinstance(name, str) for name in names)
                or len(names) != len(FEATURE_FIELDS) or set(names) != set(FEATURE_FIELDS)):
            raise ValueError(f"legacy_models {stage} model must contain the shared referral features")
        for key in ("mean", "scale", "coefficients"):
            values = model.get(key)
            if not isinstance(values, list) or len(values) != len(names) or not all(map(_finite_number, values)):
                raise ValueError(f"legacy_models {stage} model has invalid {key}")
        if any(scale <= 0 for scale in model["scale"]) or not _finite_number(model.get("intercept")):
            raise ValueError(f"legacy_models {stage} model needs positive scales and a finite intercept")
    return bundle, {"sha256": hashlib.sha256(source).hexdigest(), "training_cutoff": cutoff.isoformat()}


def _datetime_at(start: datetime, days: float):
    return (start + timedelta(days=days)).isoformat()


def _user_id(index: int):
    return f"user-{index:09d}"


def _item_id(index: int):
    return f"item-{index:04d}"


def _profile_records(table, profiles, start, make_id):
    for index, (arrival, skill, salary, occupation, region, remote, affinity) in enumerate(profiles):
        yield dict(zip(table.fields, (
            make_id(index), _datetime_at(start, float(arrival)), float(skill), float(salary),
            int(occupation), int(region), int(remote), float(affinity),
        ), strict=True))


def _event_records(rows, seekers, jobs, start, bundle, progress):
    from .trained_scores import predict_scores

    application_base, screening_base, _ = Config().base_probabilities
    for offset in range(0, len(rows), BATCH_SIZE):
        batch = rows[offset:offset + BATCH_SIZE]
        observed = features(batch, seekers, jobs)
        events = [dict(zip(FEATURE_FIELDS, values, strict=True)) for values in zip(
            *(observed[field].tolist() for field in FEATURE_FIELDS), strict=True,
        )]
        # The market uses homogeneous float profile arrays, while these columns are flags.
        for event in events:
            for field in ("wants_remote", "allows_remote", "remote_mismatch"):
                event[field] = int(event[field])
        try:
            with np.errstate(over="raise", invalid="raise", divide="raise"):
                scores = predict_scores(events, bundle)
        except FloatingPointError as exc:
            raise ValueError("legacy_models produced invalid market scores") from exc
        if not np.isfinite(scores).all():
            raise ValueError("legacy_models produced invalid market scores")
        for row, event, score in zip(batch, events, scores.tolist(), strict=True):
            seeker, job, day = (int(row[key]) for key in ("seeker", "job", "day"))
            event_id = f"market-{seeker:09d}-{job:04d}"
            event.update(event_id=event_id, user_id=_user_id(seeker), item_id=_item_id(job),
                         shoukai_dt=_datetime_at(start, day + INTRODUCTION_OFFSET_DAYS))
            for field, stage in zip(DATE_COLUMNS, STAGES, strict=True):
                value = float(row[stage])
                event[field] = _datetime_at(start, value) if math.isfinite(value) else None
            event.update(zip(SCORE_FIELDS, score, strict=True))

            pa, ps, pd = (float(row[key]) for key in ("pa", "ps", "pd"))
            truth = dict.fromkeys(TRUTH_TABLE.fields)
            truth.update(
                event_id=event_id, p_application_eventual=pa, p_screening_eventual=ps, p_decision_eventual=pd,
                seeker_affinity=float(seekers[seeker, 6]), employer_affinity=float(jobs[job, 6]),
                application_screening_gap=pa - ps,
                relative_application_screening_gap=pa / application_base - ps / screening_base,
            )
            for stage in STAGES:
                truth[f"p_candidate_{stage}"] = float(row[f"p_candidate_{stage}"])
                truth[f"p_employer_{stage}"] = float(row[f"p_employer_{stage}"])
            failure_stage = int(row["failure_stage"])
            truth["failure_stage"] = "" if failure_stage == 0 else STAGES[failure_stage - 1]
            truth["failure_reason"] = "" if failure_stage == 0 else FAILURE_REASONS[int(row["failure_reason"])]
            for column in DATE_COLUMNS:
                value = float(row[f"latent_{column}"])
                truth[f"latent_{column}"] = _datetime_at(start, value) if math.isfinite(value) else None
            yield event, truth
        if progress is not None:
            progress(offset + len(batch))


def write_market_dataset(
    config: MarketConfig,
    output: str | Path,
    *,
    market_start: str,
    legacy_models: str | Path,
    output_format: str = "sqlite",
    retain: str = "all",
    progress: Callable[[int], None] | None = None,
):
    """Simulate a market and publish its data scored with a saved ordinary funnel model bundle."""
    start = parse_datetime(market_start)
    end = _datetime_at(start, config.days)
    bundle, model_source = _load_models(Path(legacy_models), start)
    with dataset_staging(output, output_format) as staging:
        daily: list[tuple[int, int, int, int]] = []
        rows, seekers, jobs = simulate(config, retain=retain, daily_stats=daily)
        extra_tables = (
            (SEEKER_TABLE, _profile_records(SEEKER_TABLE, seekers, start, _user_id)),
            (JOB_TABLE, _profile_records(JOB_TABLE, jobs, start, _item_id)),
            (DAILY_TABLE, (dict(zip(DAILY_TABLE.fields, (_datetime_at(start, day), active_s, active_j, referrals),
                                   strict=True)) for day, active_s, active_j, referrals in daily)),
        )
        files = write_rows(staging, output_format, (MARKET_EVENT_TABLE, TRUTH_TABLE),
                           _event_records(rows, seekers, jobs, start, bundle, progress), extra_tables=extra_tables)
        manifest = {
            "generator": "synthetic-funnel", "version": VERSION, "mode": "market", "output_format": output_format,
            "config": asdict(config), "market_start": start.isoformat(), "observation_end_exclusive": end,
            "retention": retain,
            "row_counts": {"events": len(rows), "truth": len(rows), "seekers": len(seekers), "jobs": len(jobs),
                           "daily": len(daily)},
            "legacy_models": {"method": bundle["method"], **model_source},
            "probability_scope": "Conditional on the previous phase, evaluated at introduction before future exits "
                                 "or posting expiry; observed dates reflect both market exits and the observation end.",
            "truth_scope": "Counterfactual: one-sided probabilities, latent arrival dates and failure stage/reason "
                           "record the funnel continuation ignoring market exits, posting expiry and cancellations; "
                           "stages the counterfactual never reaches stay NULL.",
            "files": {name: file_hash(staging / name) for name in files},
        }
        (staging / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                                               encoding="utf-8")
    return manifest
