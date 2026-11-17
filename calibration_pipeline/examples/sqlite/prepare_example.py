"""Generate the example's candidate CSV and SQLite tables with synthetic_funnel."""

import argparse
import csv
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from dataclasses import asdict, replace
from datetime import datetime
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory

from calibration_pipeline.config import ITEM_ID_BASE, USER_ID_BASE

HERE = Path(__file__).resolve().parent


def local_time(value: str) -> str:
    return datetime.fromisoformat(value).replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")


def prepare(train_inputs: Path, inference_inputs: Path, generator_root: Path):
    source_dir = generator_root / "src"
    if not (source_dir / "synthetic_funnel").is_dir():
        source_dir = generator_root
    sys.path.insert(0, str(source_dir.resolve()))
    generator = import_module("synthetic_funnel")
    scores = import_module("synthetic_funnel.trained_scores")
    history = generator.Config(
        rows=1200, seed=42, referral_start="2024-01-01T00:00:00+09:00",
        referral_end="2024-09-30T23:59:59+09:00", as_of="2025-01-01T00:00:00+09:00",
        base_probabilities=(0.20, 0.30, 0.25), probability_half_widths=(0.10, 0.15, 0.10),
        reference_rows=6000, legacy_scores=generator.LegacyScores(training_rows=4000),
    )
    inference_at = "2025-02-01T09:00:00+09:00"
    candidates = replace(
        history, rows=8, seed=43, referral_start=inference_at, referral_end=inference_at, as_of=inference_at,
        legacy_scores=replace(history.legacy_scores, enabled=False),
    )
    candidate_rows = [row for row, _ in generator.generate(candidates)]
    # Score candidates with the same past-only model used to score the histories.
    bundle = scores.fit_models(history)
    candidate_scores = scores.predict_scores(candidate_rows, bundle)
    inference_day = local_time(inference_at)[:10]
    for row in candidate_rows:
        row["user_id"] = USER_ID_BASE + history.rows + int(row["user_id"].removeprefix("user-"))
        row["item_id"] = ITEM_ID_BASE + history.population.job_count + int(row["item_id"].removeprefix("item-"))

    train_inputs.mkdir(parents=True, exist_ok=True)
    inference_inputs.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=train_inputs) as temporary, TemporaryDirectory(dir=inference_inputs) as candidate_temporary:
        staging = Path(temporary)
        dataset = staging / "history"
        history_manifest = generator.write_dataset(history, dataset, output_format="sqlite")
        warehouse = (dataset / "dataset.sqlite.db").rename(staging / "sqlite_fixture.db")
        with closing(sqlite3.connect(warehouse)) as database, database:
            database.executescript((HERE / "schema.sql").read_text(encoding="utf-8"))
            database.executemany(
                "INSERT INTO user_feature_daily VALUES (?, ?, ?)",
                ((row["user_id"], local_time(row["shoukai_dt"])[:10], row["seeker_skill"])
                 for row in candidate_rows),
            )
        original = Path(candidate_temporary) / "original_score.csv"
        with original.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(("user_id", "item_id", "oubo_score", "shoukai_dt"))
            writer.writerows(
                (row["user_id"], row["item_id"], float(score[0]), inference_day)
                for row, score in zip(candidate_rows, candidate_scores, strict=True)
            )
        manifest = {
            "generator_version": generator.__version__, "history": asdict(history), "candidates": asdict(candidates),
            "history_export": history_manifest,
            "score_training": bundle["training_config"],
            "sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in (original, warehouse)},
        }
        metadata = staging / "synthetic_data_manifest.json"
        metadata.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        original.replace(inference_inputs / original.name)
        warehouse.replace(train_inputs / warehouse.name)
        metadata.replace(train_inputs / metadata.name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train-input-dir", type=Path, default=HERE / "train")
    parser.add_argument("--inference-input-dir", type=Path, default=HERE / "inference")
    parser.add_argument("--generator-root", type=Path, default=HERE.parents[2] / "synthetic_funnel")
    args = parser.parse_args()
    prepare(args.train_input_dir, args.inference_input_dir, args.generator_root)
