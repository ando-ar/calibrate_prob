"""Generate the example's candidate CSV and SQLite fixture with synthetic_funnel.

The generator is market-only: a single CLI run produces a SQLite warehouse whose
models are learned from the market's own warmup referrals. Generation is isolated
in :func:`run_generator` so the CLI invocation can change without touching the
fixture assembly below.
"""

import argparse
import csv
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from calibration_pipeline.config import ITEM_ID_BASE, USER_ID_BASE

HERE = Path(__file__).resolve().parent
SCORE_COLUMN = "model_apply_score"
CANDIDATE_ROWS = 8

# A single market run. The 8x8 occupation/region pools make introductions and
# decisions sparse, so a long warmup is needed for the in-market model to learn;
# the 20/40-day stays let all three stages mature inside the three-month label
# horizon. This yields ~58k events and a ~24MB SQLite warehouse.
MARKET_CONFIG = {
    "market_start": "2024-01-01T00:00:00+09:00",
    "days": 600,
    "warmup": 400,
    "seed": 71,
    "seeker_arrivals": 7,
    "job_arrivals": 7,
    "application_days": 20,
    "posting_days": 40,
    "referrals": 1,
}


def local_time(value: str) -> str:
    return datetime.fromisoformat(value).replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")


def example_id(value: str, prefix: str, base: int) -> int:
    return base + int(value.removeprefix(prefix))


def run_generator(generator_root: Path, run_dir: Path, config: dict) -> Path:
    """Run the market-only synthetic_funnel CLI and return its output directory."""
    input_dir = run_dir / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    (input_dir / "config.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    source_dir = generator_root / "src"
    if not (source_dir / "synthetic_funnel").is_dir():
        source_dir = generator_root
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(source_dir.resolve()), env.get("PYTHONPATH", ""))))
    command = [sys.executable, "-m", "synthetic_funnel", "--run-dir", str(run_dir.resolve()), "--format", "sqlite"]
    subprocess.run(command, cwd=generator_root, env=env, check=True)
    return run_dir / "output"


def prepare(train_inputs: Path, inference_inputs: Path, generator_root: Path):
    generator_root = generator_root.resolve()
    train_inputs.mkdir(parents=True, exist_ok=True)
    inference_inputs.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=train_inputs) as temporary:
        staging = Path(temporary)
        market_output = run_generator(generator_root, staging / "market", MARKET_CONFIG)
        warehouse = market_output / "dataset.sqlite.db"
        with closing(sqlite3.connect(warehouse)) as database, database:
            database.executescript((HERE / "schema.sql").read_text(encoding="utf-8"))
            candidates = database.execute(
                f"SELECT user_id, item_id, {SCORE_COLUMN}, shoukai_dt FROM events "
                f"WHERE {SCORE_COLUMN} IS NOT NULL "
                f"ORDER BY shoukai_dt DESC, event_id DESC LIMIT ?",
                (CANDIDATE_ROWS,),
            ).fetchall()
        if len(candidates) != CANDIDATE_ROWS:
            raise ValueError(f"expected {CANDIDATE_ROWS} scored market referrals, found {len(candidates)}")

        original = staging / "original_score.csv"
        with original.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(("user_id", "item_id", SCORE_COLUMN, "shoukai_dt"))
            writer.writerows(
                (example_id(user_id, "user-", USER_ID_BASE), example_id(item_id, "item-", ITEM_ID_BASE), score,
                 local_time(shoukai_dt)[:10])
                for user_id, item_id, score, shoukai_dt in candidates
            )

        market_manifest = json.loads((market_output / "manifest.json").read_text(encoding="utf-8"))
        manifest = {
            "generator_version": market_manifest["version"],
            "market": {key: market_manifest[key] for key in
                       ("config", "market_start", "observation_end_exclusive", "warmup_end",
                        "training_cutoff", "training", "row_counts")},
            "sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in (original, warehouse)},
        }
        metadata = staging / "synthetic_data_manifest.json"
        metadata.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        original.replace(inference_inputs / original.name)
        warehouse.replace(train_inputs / "sqlite_fixture.db")
        metadata.replace(train_inputs / metadata.name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train-input-dir", type=Path, default=HERE / "train")
    parser.add_argument("--inference-input-dir", type=Path, default=HERE / "inference")
    parser.add_argument("--generator-root", type=Path, default=HERE.parents[2] / "synthetic_funnel")
    args = parser.parse_args()
    prepare(args.train_input_dir, args.inference_input_dir, args.generator_root)
