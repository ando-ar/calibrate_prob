"""Organizer-side competition evaluator (issue #35, COMPETITION.md §4-§6).

The evaluator is never shipped to participants.  It takes a distributed dataset
directory (``manifest.json`` plus tables), a participant submission and an output
directory, re-runs the market under the submitted ranking policy and writes
``metrics.json``, ``daily.csv``, ``ranked.parquet`` and a compact ``summary.md``.

Metrics follow the existing ``macro_daily_*`` convention of
``product_prototype/docs/DATA_AND_EVALUATION.md``: per-day sums, a daily mean per
partition, then an equal-weight mean across partitions.  The primary metric is
the *observed* fee a policy causes in the replayed market, not the counterfactual
``p x fee`` estimate.  Undecided rows stay undecided (NULL) and never enter a fee
sum as an implicit zero.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
import sys
from collections import defaultdict
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from .export import SQLITE_FILENAME
from .market import MarketConfig, ReplayIntroductions, simulate
from .shared import deadline, file_hash, parse_datetime

SCHEMA_VERSION = 1
DEFAULT_HORIZON = "3_calendar_months"  # COMPETITION.md §11 recommendation #2
DEFAULT_MAX_TRAINING_ROWS = 300000     # §11 #3: participant constraint, NOT enforced here
DEFAULT_FALLBACK_SEEDS = 20            # §11 #4 / §5.5
EVALUATION_SCOPE = {
    "primary": "replay_policy_dependent_observed_fee",
    "logged": "logged_cohort_only_not_counterfactual_revenue",
    "counterfactual": "organizer_only_candidates_latent_not_for_ranking",
    "undecided_policy": "undecided_rows_excluded_not_zero",
    "fallback": False,
}
FALLBACK_SCOPE = {
    **EVALUATION_SCOPE,
    "primary": "replay_unavailable_single_day_seed_amplified",
    "fallback": True,
}


@dataclass(frozen=True)
class CompetitionConfig:
    """Run settings adopted from COMPETITION.md §2.1, §3.3 and §11."""

    horizon: object = DEFAULT_HORIZON       # "3_calendar_months", "120_days" or an int day count
    max_per_user_per_day: int = 5
    daily_budget_per_active_user: int = 5
    daily_budget: object = None
    partition_field: object = None          # column name in candidates/events, or None
    max_training_rows: int = DEFAULT_MAX_TRAINING_ROWS
    fallback_seeds: int = DEFAULT_FALLBACK_SEEDS

    def __post_init__(self):
        if isinstance(self.horizon, str):
            if self.horizon not in ("3_calendar_months", "120_days"):
                raise ValueError("horizon must be '3_calendar_months', '120_days' or an integer")
        elif type(self.horizon) is not int or self.horizon < 1:
            raise ValueError("horizon must be '3_calendar_months', '120_days' or a positive integer")
        for key in ("max_per_user_per_day", "daily_budget_per_active_user", "fallback_seeds"):
            value = getattr(self, key)
            if type(value) is not int or value < 1:
                raise ValueError(f"{key} must be a positive integer")
        if self.daily_budget is not None and (type(self.daily_budget) is not int or self.daily_budget < 1):
            raise ValueError("daily_budget must be a positive integer or null")
        if type(self.max_training_rows) is not int or self.max_training_rows < 1:
            raise ValueError("max_training_rows must be a positive integer")

    @property
    def horizon_days(self):
        return 120 if self.horizon == "120_days" else self.horizon

    def as_dict(self):
        return asdict(self)


def horizon_deadline(day_dt, config_or_horizon):
    """Inclusive decision deadline ``H`` after an introduction day."""
    horizon = config_or_horizon.horizon if isinstance(config_or_horizon, CompetitionConfig) else config_or_horizon
    base = parse_datetime(day_dt)
    if horizon == "3_calendar_months":
        return deadline(base)
    days = 120 if horizon == "120_days" else int(horizon)
    return base + timedelta(days=days)


def _day_offset(day_dt, start):
    value = parse_datetime(day_dt)
    if value.utcoffset() != start.utcoffset():
        raise ValueError(f"day_dt {day_dt!r} has a different UTC offset than market_start")
    delta = value - start
    if delta.total_seconds() % 86400:
        raise ValueError(f"day_dt {day_dt!r} is not a midnight day boundary")
    return delta.days


def _parse_index(value, prefix):
    text = str(value)
    if not text.startswith(prefix + "-"):
        raise ValueError(f"Invalid {prefix} id: {value!r}")
    try:
        return int(text[len(prefix) + 1:])
    except ValueError as exc:
        raise ValueError(f"Invalid {prefix} id: {value!r}") from exc


def _read_file(path):
    if path.suffix == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as stream:
            return [dict(row) for row in csv.DictReader(stream)]
    if path.suffix == ".parquet":
        import pyarrow.parquet as pq
        return pq.read_table(path).to_pylist()
    raise ValueError(f"Unsupported table file: {path}")


def read_table(directory, name, fmt="sqlite"):
    """Read a dataset table from ``<name>.<ext>`` or the dataset sqlite file."""
    directory = Path(directory)
    for ext in dict.fromkeys((fmt, "parquet", "csv")):
        path = directory / f"{name}.{ext}"
        if path.is_file():
            return _read_file(path)
    database = directory / SQLITE_FILENAME
    if database.is_file():
        with closing(sqlite3.connect(database)) as connection:
            try:
                cursor = connection.execute(f"SELECT * FROM {name}")
            except sqlite3.OperationalError as exc:
                raise FileNotFoundError(f"Table {name!r} not found in {directory}") from exc
            columns = [description[0] for description in cursor.description]
            return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
    raise FileNotFoundError(f"Table {name!r} not found in {directory}")


def load_population(manifest_dir, fmt, start):
    """Rebuild the pre-generated arrival schedule from the published tables.

    Replay keeps every arrival draw (discarding its value) so the market RNG
    stream stays aligned with the base run through ``D_train``.  The actor
    profiles themselves come from the dataset, which makes ``user_id``/``item_id``
    stable across base and replay even after a policy changes introduction
    draw counts on later evaluation days.  Only the published tables are read;
    nothing about the schedule is stored in a bulky snapshot file.
    """
    users = read_table(manifest_dir, "user", fmt)
    items = read_table(manifest_dir, "item", fmt)
    try:
        user_motivation = {row["user_id"]: float(row["user_motivation"])
                           for row in read_table(manifest_dir, "user_latent", fmt)}
    except FileNotFoundError:
        user_motivation = {}
    try:
        item_motivation = {row["item_id"]: float(row["item_motivation"])
                           for row in read_table(manifest_dir, "item_latent", fmt)}
    except FileNotFoundError:
        item_motivation = {}
    seekers = [None] * (1 + max(_parse_index(row["user_id"], "user") for row in users))
    jobs = [None] * (1 + max(_parse_index(row["item_id"], "item") for row in items))
    for row in users:
        sid = _parse_index(row["user_id"], "user")
        seekers[sid] = [
            _day_offset(str(row["arrival_dt"]), start), float(row["user_skill"]),
            float(row["desired_salary"]), row["occupation"], row["region"],
            int(row["wants_remote"]), user_motivation.get(row["user_id"], 0.0), np.nan,
        ]
    for row in items:
        jid = _parse_index(row["item_id"], "item")
        jobs[jid] = [
            _day_offset(str(row["arrival_dt"]), start), float(row["required_skill"]),
            float(row["offered_salary"]), row["occupation"], row["region"],
            int(row["allows_remote"]), item_motivation.get(row["item_id"], 0.0), np.nan,
            float(row["fee_rate"]),
        ]
    if any(profile is None for profile in seekers) or any(profile is None for profile in jobs):
        raise ValueError("Population schedule has gaps in actor indices")
    return {"seekers": seekers, "jobs": jobs}


SUBMISSION_FIELDS = ("day_dt", "user_id", "item_id", "rank")


def load_submission(path):
    """Load and validate the required submission schema (COMPETITION.md §6)."""
    rows = _read_file(Path(path))
    if not rows:
        return []
    missing = [field for field in SUBMISSION_FIELDS if field not in rows[0]]
    if missing:
        raise ValueError("Submission is missing required columns: " + ", ".join(missing))
    normalized = []
    ranks = defaultdict(set)
    for index, row in enumerate(rows):
        try:
            rank = int(row["rank"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Submission row {index} has a non-integer rank") from exc
        if any(row[field] in (None, "") for field in SUBMISSION_FIELDS):
            raise ValueError(f"Submission row {index} has an empty required value")
        item = {"day_dt": str(row["day_dt"]), "user_id": str(row["user_id"]),
                "item_id": str(row["item_id"]), "rank": rank}
        key = (item["day_dt"], item["user_id"], item["item_id"])
        if rank in ranks[item["day_dt"]]:
            raise ValueError(f"Duplicate rank {rank} on {item['day_dt']}")
        ranks[item["day_dt"]].add(rank)
        normalized.append(item)
    return normalized


@dataclass
class Allocation:
    selected: list
    audit: dict


def allocate_budget(rows, *, max_per_user_per_day=5, daily_budget_per_active_user=5,
                    daily_budget=None, partition_field=None, partition_budgets=None):
    """Apply COMPETITION.md §2.1 budget rules to submitted ranking rows.

    1. Dedup by ``(day, user, item)`` keeping the best (lowest) rank, ties by key
       ascending.  2. Keep the top ``max_per_user_per_day`` rows per user per day
       (this cap is *not* reset per partition).  3. Apply the daily total budget
       ``daily_budget_per_active_user x distinct active users``, optionally
       capped by an absolute ``daily_budget`` and per-partition budgets.
    """
    if type(max_per_user_per_day) is not int or max_per_user_per_day < 1:
        raise ValueError("max_per_user_per_day must be a positive integer")
    if type(daily_budget_per_active_user) is not int or daily_budget_per_active_user < 1:
        raise ValueError("daily_budget_per_active_user must be a positive integer")
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["day_dt"]].append(row)
    selected = []
    audit = {}
    for day in sorted(grouped):
        best = {}
        for row in grouped[day]:
            key = (row["user_id"], row["item_id"])
            order = (row["rank"], row["user_id"], row["item_id"])
            if key not in best or order < best[key][0]:
                best[key] = (order, row)
        deduped = [value[1] for value in best.values()]
        deduped.sort(key=lambda row: (row["rank"], row["user_id"], row["item_id"]))
        per_user = defaultdict(int)
        capped = []
        for row in deduped:
            user = row["user_id"]
            if per_user[user] < max_per_user_per_day:
                per_user[user] += 1
                capped.append(row)
        active_users = len({row["user_id"] for row in capped})
        total = active_users * daily_budget_per_active_user
        if daily_budget is not None:
            total = min(total, int(daily_budget))
        used = 0
        per_partition = defaultdict(int)
        final = []
        for row in capped:
            if used >= total:
                break
            partition = row.get(partition_field, "all") if partition_field else "all"
            cap = partition_budgets.get(partition, total) if partition_budgets else total
            if per_partition[partition] >= cap:
                continue
            per_partition[partition] += 1
            used += 1
            final.append(row)
        selected.extend(final)
        audit[day] = {
            "submitted": len(grouped[day]), "deduplicated": len(deduped),
            "per_user_capped": len(capped), "active_users": active_users,
            "total_budget": total, "selected": len(final),
        }
    return Allocation(selected=selected, audit=audit)


def validate_submission_keys(submission, candidates):
    """Reject keys not present in the distributed candidate set (§5.4)."""
    candidate_keys = {(str(row["day_dt"]), str(row["user_id"]), str(row["item_id"]))
                      for row in candidates}
    invalid = [(row["day_dt"], row["user_id"], row["item_id"]) for row in submission
               if (row["day_dt"], row["user_id"], row["item_id"]) not in candidate_keys]
    if invalid:
        preview = ", ".join(f"({d}, {u}, {i})" for d, u, i in invalid[:5])
        raise ValueError(f"Submission contains {len(invalid)} pairs absent from candidates: {preview}")


def _macro_equal_weight(per_partition_day):
    """Daily mean per partition, then equal-weight mean across partitions."""
    partitions = sorted({key[0] for key in per_partition_day})
    if not partitions:
        return 0.0
    means = [float(np.mean([value for (partition, _), value in per_partition_day.items()
                            if partition == p])) for p in partitions]
    return float(np.mean(means))


def _partition_of(row, field):
    return str(row.get(field, "all")) if field and row.get(field) not in (None, "") else "all"


def _event_day(events, field, start):
    """Group event rows by introduction day (shoukai_dt carries a +0.01 day offset)."""
    days = defaultdict(list)
    for row in events:
        value = row.get(field)
        if value in (None, ""):
            continue
        elapsed = (parse_datetime(str(value)) - start).total_seconds()
        days[int(elapsed // 86400)].append(row)
    return days


def evaluate(manifest_dir, submission_path, output_dir, *, config=None,
             candidates_path=None, latent_path=None, day_filter=None):
    """Run a full replay evaluation and write the four output artifacts."""
    manifest_dir = Path(manifest_dir)
    output_dir = Path(output_dir)
    manifest = json.loads((manifest_dir / "manifest.json").read_text(encoding="utf-8"))
    fmt = manifest.get("output_format", "sqlite")
    start = parse_datetime(manifest["market_start"])
    market_config = MarketConfig(**manifest["config"])
    comp = config if isinstance(config, CompetitionConfig) else CompetitionConfig(**(config or {}))
    partition_field = comp.partition_field

    submission = load_submission(submission_path)
    if day_filter is not None:
        submission = [row for row in submission if _day_offset(row["day_dt"], start) == int(day_filter)]
    if not submission:
        raise ValueError("Submission has no rows to evaluate")
    candidate_dir = Path(candidates_path).parent if candidates_path else manifest_dir
    candidate_name = Path(candidates_path).stem if candidates_path else "candidates"
    candidates = read_table(candidate_dir, candidate_name, "parquet")
    validate_submission_keys(submission, candidates)

    allocation = allocate_budget(
        submission,
        max_per_user_per_day=comp.max_per_user_per_day,
        daily_budget_per_active_user=comp.daily_budget_per_active_user,
        daily_budget=comp.daily_budget,
        partition_field=partition_field,
    )
    selected = allocation.selected
    eval_days = sorted({_day_offset(row["day_dt"], start) for row in selected})
    if not eval_days:
        raise ValueError("Budget allocation selected no pairs")
    observation_end = parse_datetime(manifest["observation_end_exclusive"])
    last_intro = start + timedelta(days=max(eval_days))
    if horizon_deadline(last_intro.isoformat(), comp) > observation_end:
        raise ValueError("D_eval_end + H exceeds observation_end_exclusive")

    key_partition = {(str(row["day_dt"]), str(row["user_id"]), str(row["item_id"])):
                     _partition_of(row, partition_field) for row in candidates}
    policy = defaultdict(list)
    for row in selected:
        policy[_day_offset(row["day_dt"], start)].append((
            _parse_index(row["user_id"], "user"), _parse_index(row["item_id"], "item")))
    replay = ReplayIntroductions(eval_start=min(eval_days), eval_end=max(eval_days) + 1,
                                 policy=dict(policy))
    population = load_population(manifest_dir, fmt, start)
    rows, seekers, jobs = simulate(market_config, replay=replay, population=population)

    replay_partition_day = defaultdict(float)
    replay_positives = 0
    selected_keys = {(row["day_dt"], row["user_id"], row["item_id"]): row for row in selected}
    by_day = defaultdict(list)
    for row in rows:
        if int(row["day"]) in eval_days:
            by_day[int(row["day"])].append(row)
    for day in eval_days:
        day_dt = (start + timedelta(days=day)).isoformat()
        limit = horizon_deadline(day_dt, comp)
        for row in by_day[day]:
            if not math.isfinite(float(row["decision"])):
                continue
            decision_dt = start + timedelta(days=float(row["decision"]))
            if decision_dt > limit:
                continue
            key = (day_dt, f"user-{int(row['seeker']):09d}", f"item-{int(row['job']):04d}")
            partition = key_partition.get(key, "all")
            replay_partition_day[partition, day] += float(row["fee_yen"])
            replay_positives += 1

    events = read_table(manifest_dir, "events", fmt)
    logged_events = _event_day(events, "shoukai_dt", start)
    logged_partition_day = defaultdict(float)
    base_decided = 0
    for day in eval_days:
        day_dt = (start + timedelta(days=day)).isoformat()
        limit = horizon_deadline(day_dt, comp)
        for row in logged_events.get(day, []):
            decision = row.get("kettei_dt")
            if decision in (None, ""):
                continue
            if parse_datetime(str(decision)) <= limit:
                base_decided += 1
                fee = row.get("fee_yen")
                if fee not in (None, "") and math.isfinite(float(fee)):
                    logged_partition_day[_partition_of(row, partition_field), day] += float(fee)

    latent_dir = Path(latent_path).parent if latent_path else manifest_dir
    latent_name = Path(latent_path).stem if latent_path else "candidates_latent"
    try:
        latent = read_table(latent_dir, latent_name, "parquet")
    except FileNotFoundError:
        latent = None
    counterfactual_partition_day = defaultdict(float)
    if latent is not None:
        latent_by_key = {}
        for entry in latent:
            latent_by_key[(str(entry.get("day_dt")), str(entry.get("user_id")),
                           str(entry.get("item_id")))] = entry
        for key in selected_keys:
            entry = latent_by_key.get(key)
            if entry is None:
                continue
            probability = entry.get("p_decision_eventual")
            fee = entry.get("fee_if_decided_yen")
            if probability not in (None, "") and fee not in (None, ""):
                day = _day_offset(key[0], start)
                counterfactual_partition_day[key_partition.get(key, "all"), day] += (
                    float(probability) * float(fee))

    metrics = {
        "macro_daily_replay_fee_yen": _macro_equal_weight(replay_partition_day),
        "total_replay_fee_yen": float(sum(replay_partition_day.values())),
        "replay_selected_positives": int(replay_positives),
        "replay_positive_capture": (float(replay_positives / base_decided) if base_decided else None),
        "macro_daily_logged_fee_yen": _macro_equal_weight(logged_partition_day),
        "macro_daily_counterfactual_fee_yen": (
            _macro_equal_weight(counterfactual_partition_day) if latent is not None else None),
    }
    daily = []
    for day in eval_days:
        day_dt = (start + timedelta(days=day)).isoformat()
        daily.append({
            "day_dt": day_dt,
            "candidates": allocation.audit[day_dt]["deduplicated"],
            "selected": allocation.audit[day_dt]["selected"],
            "replay_fee_yen": sum(value for (partition, d), value in replay_partition_day.items() if d == day),
            "logged_fee_yen": sum(value for (partition, d), value in logged_partition_day.items() if d == day),
            "counterfactual_fee_yen": (
                sum(value for (partition, d), value in counterfactual_partition_day.items() if d == day)
                if latent is not None else None),
        })

    output_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "schema_version": SCHEMA_VERSION,
        "metrics": metrics,
        "evaluation_scope": dict(EVALUATION_SCOPE),
        "config": comp.as_dict(),
        "data_version": {"manifest_sha256": file_hash(manifest_dir / "manifest.json"),
                         "generator_version": manifest.get("version")},
        "evaluation": {
            "eval_days": [(start + timedelta(days=day)).isoformat() for day in eval_days],
            "horizon": comp.horizon,
            "horizon_end": horizon_deadline((start + timedelta(days=max(eval_days))).isoformat(), comp).isoformat(),
            "observation_end_exclusive": manifest["observation_end_exclusive"],
            "budget_audit": allocation.audit,
            "replay_skipped_pairs": len(replay.skipped),
            "replay_skip_reasons": {reason: sum(1 for skip in replay.skipped if skip[3] == reason)
                                    for reason in sorted({skip[3] for skip in replay.skipped})},
        },
        "fallback": None,
    }
    (output_dir / "metrics.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                                             encoding="utf-8")
    with (output_dir / "daily.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(daily[0]))
        writer.writeheader()
        writer.writerows(daily)
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
        table = pa.Table.from_pylist([
            {**row, "selected": 1} for row in selected
        ])
        pq.write_table(table, output_dir / "ranked.parquet")
    except ImportError:  # pragma: no cover - pyarrow is a hard dependency
        pass
    summary = [
        "# Competition evaluation summary", "",
        f"- Primary (replay observed fee): **{metrics['macro_daily_replay_fee_yen']:.2f}** JPY/day",
        f"- Logged observed fee: {metrics['macro_daily_logged_fee_yen']:.2f} JPY/day",
        f"- Counterfactual diagnostic: "
        f"{'n/a' if metrics['macro_daily_counterfactual_fee_yen'] is None else format(metrics['macro_daily_counterfactual_fee_yen'], '.2f')} JPY/day",
        f"- Selected positives: {metrics['replay_selected_positives']}",
        f"- Eval days: {len(eval_days)}", "",
        "| day_dt | selected | replay_fee_yen | logged_fee_yen |",
        "| --- | ---: | ---: | ---: |",
    ]
    for row in daily:
        summary.append(f"| {row['day_dt']} | {row['selected']} | {row['replay_fee_yen']:.2f} | "
                       f"{row['logged_fee_yen']:.2f} |")
    (output_dir / "summary.md").write_text("\n".join(summary) + "\n", encoding="utf-8")
    return result


def evaluate_fallback(runs, output_dir, *, config=None, day=None):
    """Single-day, seed-amplified fallback (COMPETITION.md §5.5).

    ``runs`` is a list of ``{"manifest": ..., "submission": ...}`` mappings, one
    per independent market seed.  Each run is evaluated for a single day and the
    primary metric is aggregated as mean +/- standard deviation across seeds.
    """
    results = []
    for index, run in enumerate(runs):
        target = Path(output_dir) / f"seed_{index}"
        single_day = run.get("day", day)
        if single_day is None:
            raise ValueError("Fallback requires a single evaluation day per run ('day' in spec or --day)")
        metric = evaluate(run["manifest"], run["submission"], target, config=config,
                          day_filter=single_day)
        results.append(float(metric["metrics"]["macro_daily_replay_fee_yen"]))
    if not results:
        raise ValueError("Fallback requires at least one seed run")
    values = np.asarray(results, dtype=float)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "schema_version": SCHEMA_VERSION,
        "metrics": {
            "macro_daily_replay_fee_yen": float(values.mean()),
            "replay_fee_yen_std": float(values.std(ddof=0)),
            "macro_daily_replay_fee_yen_per_seed": results,
        },
        "evaluation_scope": dict(FALLBACK_SCOPE),
        "evaluation": {
            "seeds": len(results),
            "recommended_min_seeds": DEFAULT_FALLBACK_SEEDS,
            "single_day": True,
        },
    }
    (output_dir / "metrics.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                                             encoding="utf-8")
    (output_dir / "summary.md").write_text(
        "# Fallback evaluation (single day, seed amplified)\n\n"
        f"- Primary: {values.mean():.2f} +/- {values.std(ddof=0):.2f} JPY/day over {len(results)} seeds\n"
        f"- Scope: {FALLBACK_SCOPE['primary']}\n"
        f"- This metric must not be compared directly with the full replay primary metric.\n",
        encoding="utf-8")
    return result


def _load_config(path):
    if path is None:
        return None
    values = json.loads(Path(path).read_text(encoding="utf-8"))
    return CompetitionConfig(**values)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="synthetic-competition",
                                     description="Organizer-side synthetic funnel competition evaluator")
    commands = parser.add_subparsers(dest="command", required=True)
    evaluate_parser = commands.add_parser("evaluate", help="Replay a submission and write metrics")
    evaluate_parser.add_argument("--manifest", type=Path, required=True, help="Dataset directory")
    evaluate_parser.add_argument("--submission", type=Path, required=True, help="submission.parquet/.csv")
    evaluate_parser.add_argument("--output", type=Path, required=True, help="Output directory")
    evaluate_parser.add_argument("--config", type=Path, help="Optional JSON run config")
    evaluate_parser.add_argument("--candidates", type=Path, help="Override candidates table path")
    evaluate_parser.add_argument("--candidates-latent", type=Path, help="Override organizer-only latent table")
    evaluate_parser.add_argument("--day", type=int, help="Evaluate only this day offset (fallback helper)")
    fallback_parser = commands.add_parser("evaluate-fallback",
                                          help="Single-day evaluation amplified over independent seeds")
    fallback_parser.add_argument("--spec", type=Path, required=True,
                                 help="JSON list of {manifest, submission, [day]} per seed")
    fallback_parser.add_argument("--output", type=Path, required=True)
    fallback_parser.add_argument("--config", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "evaluate":
            result = evaluate(args.manifest, args.submission, args.output, config=_load_config(args.config),
                              candidates_path=args.candidates, latent_path=args.candidates_latent,
                              day_filter=args.day)
            print(json.dumps({"metrics": result["metrics"], "output": str(args.output.resolve())}))
        else:
            runs = json.loads(args.spec.read_text(encoding="utf-8"))
            result = evaluate_fallback(runs, args.output, config=_load_config(args.config))
            print(json.dumps({"metrics": result["metrics"], "output": str(args.output.resolve())}))
    except (ValueError, TypeError, OSError, KeyError, FileNotFoundError) as exc:
        parser.exit(2, f"error: {exc}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
