"""Publish continuous market data in the shared referral dataset formats."""

import json
import math
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

from .export import BATCH_SIZE, Table, dataset_staging, write_rows
from .market import (
    FAILURE_REASONS,
    INTRODUCTION_OFFSET_DAYS,
    MarketConfig,
    features,
    simulate,
)
from .market_training import score_warmup_referrals
from .shared import (
    BASE_PROBABILITIES,
    DATE_COLUMNS,
    FEATURE_FIELDS,
    MODEL_SCORE_FIELDS,
    STAGES,
    VERSION,
    file_hash,
    parse_datetime,
)

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
            str(occupation), str(region), int(remote), float(affinity),
            None if not math.isfinite(departure) else _datetime_at(start, float(departure)),
        ), strict=True))


def _event_records(rows, seekers, jobs, start, scores, progress):
    """Yield event/events_latent pairs scored from in-market warmup learning."""
    application_base, screening_base, _ = BASE_PROBABILITIES
    for offset in range(0, len(rows), BATCH_SIZE):
        batch = rows[offset:offset + BATCH_SIZE]
        observed = features(batch, seekers, jobs)
        for index, row in enumerate(batch):
            seeker, job, day = (int(row[key]) for key in ("seeker", "job", "day"))
            event_id = f"market-{seeker:09d}-{job:04d}"
            # Immutable attributes live in the user/item masters, not in events.
            event = {"event_id": event_id, "user_id": _user_id(seeker), "item_id": _item_id(job)}
            for field in ("skill_match", "skill_shortfall", "overqualification", "salary_shortfall"):
                event[field] = float(observed[field][index])
            for field in ("remote_mismatch", "other_applications", "competing_candidates"):
                event[field] = int(observed[field][index])
            event["shoukai_dt"] = _datetime_at(start, day + INTRODUCTION_OFFSET_DAYS)
            for field, stage in zip(DATE_COLUMNS, STAGES, strict=True):
                value = float(row[stage])
                event[field] = _datetime_at(start, value) if math.isfinite(value) else None
            event.update(zip(MODEL_SCORE_FIELDS,
                             (float(value) if math.isfinite(value) else None
                              for value in scores[offset + index]), strict=True))

            pa, ps, pd = (float(row[key]) for key in ("pa", "ps", "pd"))
            truth = dict.fromkeys(EVENTS_LATENT_TABLE.fields)
            truth.update(
                event_id=event_id, p_application_eventual=pa, p_screening_eventual=ps, p_decision_eventual=pd,
                user_affinity=float(seekers[seeker, 6]), item_affinity=float(jobs[job, 6]),
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
    output_format: str = "sqlite",
    progress: Callable[[int], None] | None = None,
):
    """Simulate a market, score every referral via warmup training and publish it."""
    started = time.perf_counter()
    start = parse_datetime(market_start)
    end = _datetime_at(start, config.days)
    with dataset_staging(output, output_format) as staging:
        daily: list[tuple[int, int, int, int, int, int, int, int, int]] = []
        rows, seekers, jobs = simulate(config, daily_stats=daily)
        scores, training_info = score_warmup_referrals(rows, seekers, jobs, config, start)
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
                           _event_records(rows, seekers, jobs, start, scores, progress),
                           extra_tables=extra_tables)
        manifest = {
            "generator": "synthetic-funnel", "version": VERSION, "mode": "market", "output_format": output_format,
            "config": json.loads(json.dumps(asdict(config))), "market_start": start.isoformat(),
            "observation_end_exclusive": end,
            "row_counts": {"events": len(rows), "events_latent": len(rows), "user": len(seekers),
                           "item": len(jobs), "daily": len(daily)},
            "training": training_info,
            "training_cutoff": training_info["training_cutoff"],
            "warmup_end": training_info["warmup_end"],
            "generation_seconds": time.perf_counter() - started,
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
