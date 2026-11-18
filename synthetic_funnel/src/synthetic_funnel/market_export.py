"""Publish continuous market data in the shared referral dataset formats."""

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from .export import BATCH_SIZE, Table, dataset_staging, write_rows
from .generator import (
    DATE_COLUMNS,
    FEATURE_FIELDS,
    STAGES,
    VERSION,
    Config,
    file_hash,
    parse_datetime,
)
from .market import INTRODUCTION_OFFSET_DAYS, MarketConfig, features, simulate

USER_TABLE = Table(
    "user", ("user_id", "arrival_dt", "user_skill", "desired_salary", "occupation", "region",
             "wants_remote", "user_affinity", "departure_dt"), "user_id",
)
ITEM_TABLE = Table(
    "item", ("item_id", "arrival_dt", "required_skill", "offered_salary", "occupation", "region",
             "allows_remote", "item_affinity", "departure_dt"), "item_id",
)
DAILY_TABLE = Table("daily", ("day_dt", "live_users", "live_items", "referrals", "new_users",
                              "new_items", "applications", "screenings", "decisions"), "day_dt")
MODEL_SCORE_FIELDS = ("model_apply_score", "model_screen_score", "model_decision_score")
EVENTS_TABLE = Table(
    "events", ("event_id", "user_id", "item_id", "skill_match", "skill_shortfall",
               "overqualification", "salary_shortfall", "remote_mismatch", "other_applications",
               "competing_candidates", *MODEL_SCORE_FIELDS, "shoukai_dt", *DATE_COLUMNS),
    "event_id",
    (("user_id", USER_TABLE.name, USER_TABLE.key), ("item_id", ITEM_TABLE.name, ITEM_TABLE.key)),
)
EVENTS_LATENT_TABLE = Table(
    "events_latent",
    ("event_id", *(f"p_{stage}_eventual" for stage in STAGES),
     *(f"p_candidate_{stage}" for stage in STAGES), *(f"p_employer_{stage}" for stage in STAGES),
     "user_affinity", "item_affinity", "failure_stage", "failure_reason",
     "application_screening_gap", "relative_application_screening_gap",
     *(f"latent_{column}" for column in DATE_COLUMNS)),
    "event_id", (("event_id", EVENTS_TABLE.name, EVENTS_TABLE.key),),
)


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
    for index, (arrival, skill, salary, occupation, region, remote, affinity, departure) in enumerate(profiles):
        yield dict(zip(table.fields, (
            make_id(index), _datetime_at(start, float(arrival)), float(skill), float(salary),
            int(occupation), int(region), int(remote), float(affinity),
            None if not math.isfinite(departure) else _datetime_at(start, float(departure)),
        ), strict=True))


def _event_records(rows, seekers, jobs, start, bundle, progress):
    from .trained_scores import predict_scores

    application_base, screening_base, _ = Config().base_probabilities
    for offset in range(0, len(rows), BATCH_SIZE):
        batch = rows[offset:offset + BATCH_SIZE]
        observed = features(batch, seekers, jobs)
        scored = [dict(zip(FEATURE_FIELDS, values, strict=True)) for values in zip(
            *(observed[field].tolist() for field in FEATURE_FIELDS), strict=True,
        )]
        # The market uses homogeneous float profile arrays, while these columns are flags.
        for event in scored:
            for field in ("wants_remote", "allows_remote", "remote_mismatch"):
                event[field] = int(event[field])
        try:
            with np.errstate(over="raise", invalid="raise", divide="raise"):
                scores = predict_scores(scored, bundle)
        except FloatingPointError as exc:
            raise ValueError("legacy_models produced invalid market scores") from exc
        if not np.isfinite(scores).all():
            raise ValueError("legacy_models produced invalid market scores")
        for local, (row, score) in enumerate(zip(batch, scores.tolist(), strict=True)):
            seeker, job, day = (int(row[key]) for key in ("seeker", "job", "day"))
            event_id = f"market-{seeker:09d}-{job:04d}"
            # Immutable attributes live in the user/item masters, not in events.
            event = {"event_id": event_id, "user_id": _user_id(seeker), "item_id": _item_id(job)}
            for field in ("skill_match", "skill_shortfall", "overqualification", "salary_shortfall"):
                event[field] = float(observed[field][local])
            for field in ("remote_mismatch", "other_applications", "competing_candidates"):
                event[field] = int(observed[field][local])
            event.update(zip(MODEL_SCORE_FIELDS, score, strict=True))
            event["shoukai_dt"] = _datetime_at(start, day + INTRODUCTION_OFFSET_DAYS)
            for field, stage in zip(DATE_COLUMNS, STAGES, strict=True):
                value = float(row[stage])
                event[field] = _datetime_at(start, value) if math.isfinite(value) else None

            pa, ps, pd = (float(row[key]) for key in ("pa", "ps", "pd"))
            truth = dict.fromkeys(EVENTS_LATENT_TABLE.fields)
            truth.update(
                event_id=event_id, p_application_eventual=pa, p_screening_eventual=ps, p_decision_eventual=pd,
                user_affinity=float(seekers[seeker, 6]), item_affinity=float(jobs[job, 6]),
                application_screening_gap=pa - ps,
                relative_application_screening_gap=pa / application_base - ps / screening_base,
            )
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
        daily: list[tuple[int, int, int, int, int, int, int, int, int]] = []
        rows, seekers, jobs = simulate(config, retain=retain, daily_stats=daily)
        extra_tables = (
            (USER_TABLE, _profile_records(USER_TABLE, seekers, start, _user_id)),
            (ITEM_TABLE, _profile_records(ITEM_TABLE, jobs, start, _item_id)),
            (DAILY_TABLE, (dict(zip(DAILY_TABLE.fields,
                                    (_datetime_at(start, day), users, items, referrals, new_users,
                                     new_items, applications, screenings, decisions), strict=True))
                           for day, users, items, referrals, new_users, new_items,
                               applications, screenings, decisions in daily)),
        )
        files = write_rows(staging, output_format, (EVENTS_TABLE, EVENTS_LATENT_TABLE),
                           _event_records(rows, seekers, jobs, start, bundle, progress), extra_tables=extra_tables)
        manifest = {
            "generator": "synthetic-funnel", "version": VERSION, "mode": "market", "output_format": output_format,
            "config": asdict(config), "market_start": start.isoformat(), "observation_end_exclusive": end,
            "retention": retain,
            "row_counts": {"events": len(rows), "events_latent": len(rows), "user": len(seekers),
                           "item": len(jobs), "daily": len(daily)},
            "legacy_models": {"method": bundle["method"], **model_source},
            "probability_scope": "Conditional on the previous phase, evaluated at introduction before future exits "
                                 "or posting expiry; observed dates reflect both market exits and the observation end.",
            "files": {name: file_hash(staging / name) for name in files},
        }
        (staging / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                                               encoding="utf-8")
    return manifest
