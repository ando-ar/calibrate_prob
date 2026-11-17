"""Mature labels, time splits and deterministic, count-aware SQL sampling."""
from __future__ import annotations

import hashlib
import math
from pathlib import Path

import duckdb
import pandas as pd

from .config import Config, identifier
from .io import write_json, file_hash

STAGES = ("positive", "screen", "apply", "no_apply")


def allocate(capacities: dict[str, int], requested: int) -> dict[str, int]:
    """Water-fill equal group caps, reassigning unused capacity deterministically."""
    requested = min(requested, sum(capacities.values()))
    result = {key: 0 for key in sorted(capacities)}
    remaining = requested
    while remaining:
        active = [key for key in result if result[key] < capacities[key]]
        increment = max(1, remaining // len(active))
        for key in active:
            count = min(increment, capacities[key] - result[key], remaining)
            result[key] += count
            remaining -= count
            if not remaining:
                break
    return result


class Catalog:
    def __init__(self, config: Config, snapshot: Path):
        self.config = config
        self.db = duckdb.connect()
        self.db.execute("SET threads = ?", [config.section("experiment").get("threads", 2)])
        self.db.execute("SET memory_limit = ?", [config.section("experiment").get("memory_limit", "2GB")])
        temporary = config.output / "duckdb_tmp"
        temporary.mkdir(parents=True, exist_ok=True)
        self.db.execute("SET temp_directory = ?", [str(temporary)])
        self.db.read_parquet(str(snapshot)).create_view("raw")
        labels = config.section("labels")
        horizon = labels["horizon_days"]
        as_of = config.date("labels", "as_of").isoformat(sep=" ")

        def observed(column: str) -> str:
            event = identifier(column)
            return (f"({event} >= _anchor AND {event} < _anchor + INTERVAL '{horizon} days' "
                    f"AND {event} <= TIMESTAMP '{as_of}')")

        application = observed(labels["application_column"])
        screen = observed(labels["screen_column"])
        positive = observed(labels["target_column"])
        self.db.execute(f"""
            CREATE VIEW examples AS
            SELECT *, COALESCE({positive}, FALSE)::INTEGER AS _label,
                   CASE WHEN {positive} THEN 'positive'
                        WHEN {screen} THEN 'screen'
                        WHEN {application} THEN 'apply'
                        ELSE 'no_apply' END AS _stage,
                   strftime(_day, '%Y-%m') AS _month
            FROM raw
            WHERE _anchor + INTERVAL '{horizon} days' <= TIMESTAMP '{as_of}'
        """)

    def close(self) -> None:
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def interval(self, split: str) -> tuple[pd.Timestamp, pd.Timestamp]:
        keys = {"train": ("train_start", "train_end"), "validation": ("train_end", "validation_end"),
                "test": ("validation_end", "test_end")}
        start, end = (self.config.date("split", key) for key in keys[split])
        if split != "test" and self.config.section("split").get("purge_label_overlap", True):
            end -= pd.Timedelta(days=self.config.section("labels")["horizon_days"])
        return start, end

    def split_query(self, split: str) -> tuple[str, list]:
        start, end = self.interval(split)
        return "SELECT * FROM examples WHERE _anchor >= ? AND _anchor < ?", [start, end]

    def batches(self, split: str, batch_size: int = 50000):
        query, params = self.split_query(split)
        result = self.db.execute(query + " ORDER BY _anchor, _row_id", params)
        reader = result.to_arrow_reader(batch_size) if hasattr(result, "to_arrow_reader") else result.fetch_record_batch(batch_size)
        for batch in reader:
            yield batch.to_pandas()

    def describe(self) -> dict:
        result = {}
        for split in ("train", "validation", "test"):
            query, params = self.split_query(split)
            rows = self.db.execute(f"SELECT _stage, count(*) FROM ({query}) GROUP BY _stage", params).fetchall()
            result[split] = dict(rows)
        return result

    def profile(self) -> dict:
        """Training-only quality diagnostics, never used to select rows automatically."""
        query, params = self.split_query("train")
        labels = self.config.section("labels")
        result = {"scope": "training_only_observed_event_delays_not_censoring_adjusted", "events": {}}
        as_of = self.config.date("labels", "as_of")
        for key in ("application_column", "screen_column", "target_column"):
            column = identifier(labels[key])
            counts = self.db.execute(f"""SELECT count(*) AS rows,
                count(*) FILTER (WHERE {column} IS NULL) AS missing_event,
                count(*) FILTER (WHERE {column} < _anchor) AS before_introduction
                FROM ({query})""", params).fetchone()
            delays = self.db.execute(f"""SELECT count(*),
                quantile_cont(date_diff('second', _anchor, {column}) / 86400.0, [0.5, 0.9, 0.95, 0.99])
                FROM ({query}) WHERE {column} >= _anchor AND {column} <= ?""", params + [as_of]).fetchone()
            result["events"][labels[key]] = {"rows": counts[0], "null_rows": counts[1],
                "before_introduction_rows": counts[2], "observed_events": delays[0],
                "days_from_introduction_p50_p90_p95_p99": delays[1]}
        return result

    def sample(self, plan: dict, folder: Path, max_rows_override: int | None = None) -> pd.DataFrame:
        folder.mkdir(parents=True, exist_ok=True)
        start, end = self.interval("train")
        if plan.get("months"):
            start = max(start, end - pd.DateOffset(months=plan["months"]))
        groups = plan.get("balance_columns", [])
        group_sql = ("to_json(list_value(" + ", ".join(f"CAST({identifier(c)} AS VARCHAR)" for c in groups) + "))"
                     if groups else "'all'")
        pool = f"SELECT *, {group_sql} AS _stratum FROM examples WHERE _anchor >= ? AND _anchor < ?"
        # Register a materialized temporary pool name only once per sampling call.
        self.db.execute("DROP TABLE IF EXISTS sampling_counts")
        self.db.execute(f"""CREATE TEMP TABLE sampling_counts AS
            SELECT _stage, _stratum, count(*)::BIGINT AS n FROM ({pool}) GROUP BY _stage, _stratum""",
                        [start, end])
        counts = self.db.sql("SELECT * FROM sampling_counts ORDER BY _stage, _stratum").df()
        total = int(counts["n"].sum())
        if not total:
            raise ValueError(f"No rows for dataset {plan['name']}")
        budget = min(max_rows_override if max_rows_override is not None else plan.get("max_rows", total), total)
        quotas = []
        if plan["mode"] == "uniform":
            capacities = counts.groupby("_stratum")["n"].sum().astype(int).to_dict()
            for stratum, count in allocate(capacities, budget).items():
                quotas.append({"sample_group": stratum, "cap": count, "population": capacities[stratum]})
            sample_group = "_stratum"
        else:
            populations = counts.groupby("_stage")["n"].sum().astype(int).to_dict()
            positives = populations.get("positive", 0)
            if positives > budget:
                raise ValueError("All positive rows exceed max_rows. Increase the budget or shorten the window")
            stage_caps = {"positive": positives}
            remaining = budget - positives
            for stage in STAGES[1:]:
                stage_caps[stage] = min(populations.get(stage, 0),
                                        math.floor(remaining * plan["negative_shares"][stage]))
            spare = budget - sum(stage_caps.values())
            for stage in STAGES[1:]:
                extra = min(spare, populations.get(stage, 0) - stage_caps[stage])
                stage_caps[stage] += extra
                spare -= extra
            for stage in STAGES:
                selected = counts[counts["_stage"] == stage]
                capacities = dict(zip(selected["_stratum"], selected["n"].astype(int)))
                for stratum, count in allocate(capacities, stage_caps[stage]).items():
                    key = stage + ":" + stratum
                    quotas.append({"sample_group": key, "cap": count, "population": capacities[stratum]})
            sample_group = "_stage || ':' || _stratum"
        quota_frame = pd.DataFrame(quotas)
        self.db.register("quota_frame", quota_frame)
        seed = self.config.section("experiment").get("seed", 42)
        query = f"""
            WITH pool AS ({pool}), ordered AS (
                SELECT *, {sample_group} AS sample_group,
                    row_number() OVER (PARTITION BY {sample_group}
                        ORDER BY md5(_row_id || ?), _row_id) AS sample_rank
                FROM pool
            )
            SELECT ordered.* EXCLUDE (sample_group, sample_rank),
                   q.cap::DOUBLE / q.population AS _sampling_probability
            FROM ordered JOIN quota_frame q USING (sample_group)
            WHERE sample_rank <= q.cap ORDER BY _row_id
        """
        frame = self.db.execute(query, [start, end, f":seed={seed}"]).df()
        self.db.unregister("quota_frame")
        assert len(frame) == budget and frame["_row_id"].is_unique
        frame.to_parquet(folder / "training.parquet", index=False, compression="zstd")
        ids_hash = hashlib.sha256("\n".join(frame["_row_id"].astype(str)).encode()).hexdigest()
        write_json(folder / "sampling.json", {
            "plan": plan, "from_inclusive": start.isoformat(), "to_exclusive": end.isoformat(),
            "population": total, "selected": len(frame), "positive_rows": int(frame["_label"].sum()),
            "population_stage_counts": {str(k): int(v) for k, v in counts.groupby("_stage")["n"].sum().items()},
            "stage_counts": {str(k): int(v) for k, v in frame["_stage"].value_counts().items()},
            "quotas": quotas, "row_ids_sha256": ids_hash, "seed": seed,
            "file_sha256": file_hash(folder / "training.parquet"),
        })
        (folder / "sampling.sql").write_text(query, encoding="utf-8")
        return frame
