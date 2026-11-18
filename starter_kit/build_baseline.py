"""Rebuild the §7 baseline submission from the distributed participant data.

Run from the repository root (or anywhere with ``synthetic_funnel`` importable):

    uv run --project synthetic_funnel python starter_kit/build_baseline.py

Inputs (all gitignored build artifacts under ``starter_kit/data/proof/participant``):

* ``dataset.sqlite.db`` -- observed tables only (user / item / events / daily)
* ``manifest.json`` -- generator config, seed, warmup_end, data version
* ``config.json`` -- proof config and competition run config

Outputs (also gitignored, in the same directory):

* ``candidates_eval.parquet`` -- evaluation-day ``candidates`` (observed columns)
* ``baseline_model.json`` -- refitted warmup model bundle (coefficients for audit)
* ``submission.parquet`` (official) and ``submission.csv`` (accepted alternative)
* ``submission_metadata.json`` -- §6 reproducibility metadata

Baseline derivation (COMPETITION.md §7) is documented in ``README.md``. In short:
the three warmup logistic models are refitted from observed warmup referrals with
the generator's own ``train_warmup_models`` (no latent columns, no future
outcomes) and applied to evaluation-day candidates. ``p_decision_baseline`` is
the product of the three clipped conditional stage scores, the only marginal
decision probability the warmup models provide; the ordering key is
``p_decision_baseline * fee_if_decided_yen`` descending.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from importlib import metadata
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from synthetic_funnel.market import MarketConfig
from synthetic_funnel.market_candidates import candidate_rows
from synthetic_funnel.market_training import train_warmup_models
from synthetic_funnel.shared import FEATURE_FIELDS, predict_scores

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "config.json"
DEFAULT_DATA = HERE / "data" / "proof" / "participant"
SUBMISSION_COLUMNS = (
    "day_dt", "user_id", "item_id", "rank",
    "p_application", "p_screening", "p_decision", "expected_fee_yen",
)
# Only fields train_warmup_models() / features() actually read are reconstructed.
ROWS_DTYPE = np.dtype([
    ("seeker", "i4"), ("job", "i4"), ("day", "i4"),
    ("application", "f8"), ("screening", "f8"), ("decision", "f8"),
    ("other_applications", "i2"), ("competing_candidates", "i2"),
])


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def day_offset(value: str | None, start: datetime) -> float:
    if value is None:
        return math.nan
    return (datetime.fromisoformat(value) - start).total_seconds() / 86400.0


def load_warmup_bundle(connection: sqlite3.Connection, manifest: dict):
    """Refit the in-market warmup models using only observed distributed rows."""
    start = datetime.fromisoformat(manifest["market_start"])
    warmup_end = datetime.fromisoformat(manifest["warmup_end"])
    users = connection.execute("SELECT user_id, arrival_dt, user_skill, desired_salary, wants_remote FROM user ORDER BY rowid").fetchall()
    items = connection.execute("SELECT item_id, arrival_dt, required_skill, offered_salary, allows_remote FROM item ORDER BY rowid").fetchall()
    user_index = {row[0]: index for index, row in enumerate(users)}
    item_index = {row[0]: index for index, row in enumerate(items)}

    # profiles mirror simulate(): arrival, skill, salary, occupation, region,
    # remote, hidden motivation (unused here), departure, [fee_rate for items].
    seekers = np.empty((len(users), 8), dtype=object)
    for index, row in enumerate(users):
        seekers[index] = (day_offset(row[1], start), row[2], row[3], "", "", row[4], 0.0, math.nan)
    jobs = np.empty((len(items), 9), dtype=object)
    for index, row in enumerate(items):
        jobs[index] = (day_offset(row[1], start), row[2], row[3], "", "", row[4], 0.0, math.nan, manifest["config"].get("fee_rate", 0.33))

    events = connection.execute(
        "SELECT user_id, item_id, shoukai_dt, oubo_dt, shorui_tsuuka_dt, kettei_dt, "
        "other_applications, competing_candidates FROM events WHERE shoukai_dt < ? ORDER BY rowid",
        (warmup_end.isoformat(),),
    ).fetchall()
    rows = np.empty(len(events), dtype=ROWS_DTYPE)
    for index, (uid, iid, shoukai, oubo, screen, decision, other, competing) in enumerate(events):
        introduction = day_offset(shoukai, start)
        rows[index] = (
            user_index[uid], item_index[iid], int(math.floor(introduction)),
            day_offset(oubo, start), day_offset(screen, start), day_offset(decision, start),
            int(other), int(competing),
        )
    config = MarketConfig(**manifest["config"])
    bundle, info = train_warmup_models(rows, seekers, jobs, config, start)
    if bundle is None:
        raise RuntimeError("Warmup models could not be trained from the distributed data: " + json.dumps(info))
    return bundle, info, len(events)


def candidate_features(row: dict) -> dict:
    features = {
        "seeker_skill": float(row["seeker_skill"]),
        "required_skill": float(row["required_skill"]),
        "desired_salary": float(row["desired_salary"]),
        "offered_salary": float(row["offered_salary"]),
        "wants_remote": int(row["wants_remote"]),
        "allows_remote": int(row["allows_remote"]),
        "skill_match": 1 - abs(float(row["seeker_skill"]) - float(row["required_skill"])),
        "skill_shortfall": max(float(row["required_skill"]) - float(row["seeker_skill"]), 0.0),
        "overqualification": max(float(row["seeker_skill"]) - float(row["required_skill"]), 0.0),
        "salary_shortfall": float(row["salary_shortfall"]),
        "remote_mismatch": int(row["remote_mismatch"]),
        "other_applications": int(row["other_applications"]),
        "competing_candidates": int(row["competing_candidates"]),
    }
    assert tuple(features) == FEATURE_FIELDS
    return features


def build_submission(config_path: Path, data_dir: Path) -> dict:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    manifest_path = data_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    competition = config["competition"]
    rule = competition["ranking_rule"]
    eps = float(competition["clip_epsilon"])
    start = datetime.fromisoformat(manifest["market_start"])
    offsets = competition["evaluation_day_offsets"]

    with sqlite3.connect(data_dir / "dataset.sqlite.db") as connection:
        # Idempotent indexes for the correlated candidate recipe (harmless if present).
        connection.execute("CREATE INDEX IF NOT EXISTS idx_events_user ON events(user_id, oubo_dt, kettei_dt, outcome_dt)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_events_item ON events(item_id, oubo_dt, kettei_dt, outcome_dt)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_user_occupation ON user(occupation, arrival_dt, departure_dt)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_item_occupation ON item(occupation, arrival_dt, departure_dt)")
        connection.commit()
        bundle, training_info, warmup_events = load_warmup_bundle(connection, manifest)
        candidate_by_day: dict[str, list[dict]] = {}
        for offset in offsets:
            day_dt = (start + timedelta(days=int(offset))).isoformat()
            rows = candidate_rows(connection, day_dt)
            candidate_by_day[day_dt] = rows

    # Save the fitted bundle so the baseline is auditable by participants.
    (data_dir / "baseline_model.json").write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    # Flatten the observed candidate list into the distributed convenience parquet.
    flat_candidates = [row for rows in candidate_by_day.values() for row in rows]
    if flat_candidates:
        columns = list(flat_candidates[0])
        schema = pa.schema([
            (name, pa.string()) if name in ("day_dt", "user_id", "item_id") else
            (name, pa.int64()) if name in ("wants_remote", "allows_remote", "other_applications", "competing_candidates") else
            (name, pa.float64())
            for name in columns
        ])
        pq.write_table(pa.Table.from_pylist(flat_candidates, schema=schema), data_dir / "candidates_eval.parquet")

    submission: list[dict] = []
    for day_dt, rows in candidate_by_day.items():
        if not rows:
            continue
        scores = predict_scores([candidate_features(row) for row in rows], bundle)
        scored = []
        for row, (s_apply, s_screen, s_decision) in zip(rows, scores, strict=True):
            p_apply = min(max(float(s_apply), eps), 1 - eps)
            p_screen = min(max(float(s_screen), eps), 1 - eps)
            p_decision_stage = min(max(float(s_decision), eps), 1 - eps)
            p_baseline = p_apply * p_screen * p_decision_stage
            fee = float(row["fee_if_decided_yen"])
            if rule == "stage_product_times_fee":
                key = p_baseline * fee
            elif rule == "decision_times_fee":
                key = p_decision_stage * fee
            elif rule == "odds_times_fee":
                key = (p_baseline / (1 - p_baseline)) * fee
            else:
                raise ValueError("Unknown ranking_rule: " + rule)
            scored.append({
                "day_dt": day_dt,
                "user_id": row["user_id"],
                "item_id": row["item_id"],
                "p_application": p_apply,
                "p_screening": p_screen,
                "p_decision": p_baseline,
                "expected_fee_yen": key,
                "_key": key,
            })
        scored.sort(key=lambda r: (-r["_key"], r["day_dt"], r["user_id"], r["item_id"]))
        for rank, item in enumerate(scored, start=1):
            item["rank"] = rank
        submission.extend(scored)

    submission.sort(key=lambda r: (r["day_dt"], r["rank"]))
    schema = pa.schema([
        ("day_dt", pa.string()), ("user_id", pa.string()), ("item_id", pa.string()),
        ("rank", pa.int64()), ("p_application", pa.float64()), ("p_screening", pa.float64()),
        ("p_decision", pa.float64()), ("expected_fee_yen", pa.float64()),
    ])
    parquet_path = data_dir / "submission.parquet"
    pq.write_table(
        pa.Table.from_pylist([{column: row[column] for column in SUBMISSION_COLUMNS} for row in submission], schema=schema),
        parquet_path,
    )
    csv_path = data_dir / "submission.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=SUBMISSION_COLUMNS)
        writer.writeheader()
        for row in submission:
            writer.writerow({column: row[column] for column in SUBMISSION_COLUMNS})

    def package_version(name: str) -> str:
        try:
            return metadata.version(name)
        except metadata.PackageNotFoundError:
            return "unknown"

    try:
        git_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        git_commit = "unknown"

    metadata_doc = {
        "author": "starter-kit",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": git_commit,
        "seed": {"market": manifest["config"]["seed"], "participant": None, "sql": None},
        "config_sha256": sha256(config_path),
        "data_version": sha256(manifest_path),
        "generator_version": manifest["version"],
        "ranking_rule": rule,
        "baseline_derivation": config["baseline"]["p_decision_baseline"],
        "parquet_sha256": sha256(parquet_path),
        "csv_sha256": sha256(csv_path),
        "environment": {
            "python": sys.version.split()[0],
            "packages": {
                "synthetic-funnel": package_version("synthetic-funnel"),
                "numpy": package_version("numpy"),
                "pyarrow": package_version("pyarrow"),
                "scikit-learn": package_version("scikit-learn"),
            },
        },
        "evaluator_version": None,
        "metrics_path": None,
    }
    (data_dir / "submission_metadata.json").write_text(
        json.dumps(metadata_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {
        "warmup_events": warmup_events,
        "training_status": training_info["status"],
        "training_stages": training_info.get("stages", []),
        "evaluation_days": list(candidate_by_day),
        "submission_rows": len(submission),
        "ranking_rule": rule,
        "submission_parquet": str(parquet_path),
        "submission_csv": str(csv_path),
        "data_version": metadata_doc["data_version"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    args = parser.parse_args()
    print(json.dumps(build_submission(args.config, args.data), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
