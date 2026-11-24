"""competition1 release pipeline (organizer-side, issue #42).

Generates the frozen competition1 market dataset(s), derives the participant
package (observed tables only, *no* latent tables), builds the §7 baseline
submission, runs the replay evaluator, records metrics + the data-version
(manifest hash) per seed, and deletes the heavy DB/parquet artifacts.

Run from anywhere with ``synthetic_funnel`` importable:

    uv run --project synthetic_funnel python competitions/competition1/organizer/build_release.py

Production (frozen config, 200/200 arrivals x 5 seeds) is the default; the
datasets are large (one 200/200 x 280-day seed is ~2.8 GiB sqlite plus multi-GB
candidate parquet), so the storage constraint (roughly 17 GB free) is honoured
by processing each seed *sequentially* and deleting its DB/parquet before the
next seed starts.

Proof mode (``--proof``) shrinks arrivals to 50/50 and to a single seed while
keeping every other field (days/warmup/offsets/H) identical, so the whole
pipeline can be exercised end-to-end in minutes instead of ~10 min/seed.

Each seed runs in a fresh child process: sqlite/pyarrow keep the database file
locked on Windows until the process exits, so the parent does the deletion once
the worker has finished (this mirrors ``seed_batch.generate_seeds``).
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from synthetic_funnel import MarketConfig, write_market_dataset
from synthetic_funnel.market_candidates import attach_latent_truth, candidate_rows
from synthetic_funnel.market_evaluation import CompetitionConfig, evaluate

HERE = Path(__file__).resolve().parent                      # .../competition1/organizer
PARTICIPANTS_DIR = HERE.parent / "participants"              # .../competition1/participants
ORGANIZER_DATA_ROOT = HERE / "data"
PARTICIPANT_DATA_ROOT = PARTICIPANTS_DIR / "data"
METRICS_ROOT = HERE / "metrics"
DEFAULT_FROZEN_CONFIG = HERE / "frozen_config.json"

# Organizer-only tables stripped from the participant copy (COMPETITION.md §1).
LATENT_TABLES = ("user_latent", "item_latent", "events_latent")
# Latent-derived column fragments that must never appear in the participant package.
# Note: a submission's own p_application/p_screening/p_decision are legitimate diagnostic
# columns (§6) and are NOT latent; the latent truth is the counterfactual *_eventual,
# p_candidate_* / p_employer_* probabilities plus motivations and skill-derived features.
LATENT_COLUMN_FRAGMENTS = (
    "motivation", "skill_match", "skill_shortfall", "overqualification",
    "eventual", "p_candidate_", "p_employer_",
    "failure_stage", "failure_reason", "latent_",
)

CANDIDATE_STRING_FIELDS = {"day_dt", "user_id", "item_id"}
CANDIDATE_INT_FIELDS = {"wants_remote", "allows_remote", "remote_mismatch",
                        "other_applications", "competing_candidates"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def robust_rmtree(path: Path) -> None:
    path = Path(path)
    if path.exists():
        shutil.rmtree(path)


def candidate_schema(columns) -> pa.Schema:
    return pa.schema([
        (name, pa.string() if name in CANDIDATE_STRING_FIELDS else
               pa.int64() if name in CANDIDATE_INT_FIELDS else pa.float64())
        for name in columns
    ])


LATENT_PROJECTION_FIELDS = ("day_dt", "user_id", "item_id", "p_decision_eventual", "fee_if_decided_yen")


def latent_schema() -> pa.Schema:
    return pa.schema([
        ("day_dt", pa.string()), ("user_id", pa.string()), ("item_id", pa.string()),
        ("p_decision_eventual", pa.float64()), ("fee_if_decided_yen", pa.float64()),
    ])


def expand_offsets(spec) -> list[int]:
    """Accept either a {"start", "end"} half-open pair or an explicit list of offsets."""
    if isinstance(spec, dict):
        return list(range(int(spec["start"]), int(spec["end"])))
    return [int(offset) for offset in spec]


def load_frozen_config(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def market_config_for(frozen: dict, seed: int) -> tuple[MarketConfig, str]:
    market = dict(frozen["market"])
    market_start = market.pop("market_start")
    market.pop("output_format", None)  # consumed by write_market_dataset, not MarketConfig
    market["seed"] = seed
    return MarketConfig(**market), market_start


def load_baseline_module():
    """Load ``starter_kit/build_baseline.py`` as a module to reuse its logic."""
    path = HERE.parent.parent.parent / "starter_kit" / "build_baseline.py"
    spec = importlib.util.spec_from_file_location("_starter_baseline", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["_starter_baseline"] = module
    spec.loader.exec_module(module)
    return module


def write_baseline_config(path: Path, frozen: dict, seed: int, eval_offsets: list[int]) -> None:
    """Write a starter-kit-shaped config.json consumed by build_baseline.py."""
    market = dict(frozen["market"])
    market["seed"] = seed
    doc = {
        "name": frozen["name"],
        "market": market,
        "competition": {
            "horizon": frozen["competition"]["horizon"],
            "max_training_rows": frozen["competition"]["max_training_rows"],
            "max_per_user_per_day": frozen["competition"]["max_per_user_per_day"],
            "daily_budget_per_active_user": frozen["competition"]["daily_budget_per_active_user"],
            "daily_budget": frozen["competition"]["daily_budget"],
            "fee_rate": frozen["competition"]["fee_rate"],
            "ranking_rule": frozen["competition"]["ranking_rule"],
            "clip_epsilon": frozen["competition"]["clip_epsilon"],
            "evaluation_day_offsets": eval_offsets,
        },
        "baseline": frozen["baseline"],
    }
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def derive_candidates(connection: sqlite3.Connection, manifest: dict,
                      eval_offsets: list[int], out_dir: Path) -> int:
    """Write the organizer-side candidates.parquet and candidates_latent.parquet.

    Candidates are streamed per day to keep memory bounded for production-size
    candidate sets. ``candidates_latent.parquet`` is the organizer-only
    counterfactual truth (projected to what the evaluator reads).
    """
    start = datetime.fromisoformat(manifest["market_start"])
    seed = manifest["config"]["seed"]
    out_dir.mkdir(parents=True, exist_ok=True)
    obs_path = out_dir / "candidates.parquet"
    lat_path = out_dir / "candidates_latent.parquet"
    obs_writer = None
    lat_writer = None
    days = 0
    for offset in eval_offsets:
        day_dt = (start + timedelta(days=offset)).isoformat()
        observed = candidate_rows(connection, day_dt)
        observed.sort(key=lambda r: (r["day_dt"], r["user_id"], r["item_id"]))
        if observed:
            if obs_writer is None:
                obs_writer = pq.ParquetWriter(obs_path, candidate_schema(list(observed[0])))
            obs_writer.write_table(
                pa.Table.from_pylist(observed, schema=candidate_schema(list(observed[0]))))
        latent = attach_latent_truth(candidate_rows(connection, day_dt, latent=True), seed=seed)
        projection = [
            {key: row[key] for key in LATENT_PROJECTION_FIELDS}
            for row in sorted(latent, key=lambda r: (r["day_dt"], r["user_id"], r["item_id"]))
        ]
        if projection:
            if lat_writer is None:
                lat_writer = pq.ParquetWriter(lat_path, latent_schema())
            lat_writer.write_table(pa.Table.from_pylist(projection, schema=latent_schema()))
        days += 1
    if obs_writer is not None:
        obs_writer.close()
    if lat_writer is not None:
        lat_writer.close()
    return days


def build_participant_package(organizer_dir: Path, participant_dir: Path, frozen: dict,
                              seed: int, eval_offsets: list[int]) -> dict:
    """Copy the observed tables, strip latent tables, and build the baseline."""
    participant_dir.mkdir(parents=True, exist_ok=True)
    source = organizer_dir / "dataset.sqlite.db"
    target = participant_dir / "dataset.sqlite.db"
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
        for table in LATENT_TABLES:
            dst.execute(f"DROP TABLE IF EXISTS {table}")
        dst.commit()
        dst.execute("VACUUM")
    shutil.copy2(organizer_dir / "manifest.json", participant_dir / "manifest.json")
    write_baseline_config(participant_dir / "config.json", frozen, seed, eval_offsets)
    baseline = load_baseline_module()
    return baseline.build_submission(participant_dir / "config.json", participant_dir)


def assert_no_latent(participant_dir: Path) -> list[str]:
    """Assert the participant package exposes no latent table or column."""
    problems: list[str] = []
    db = participant_dir / "dataset.sqlite.db"
    with sqlite3.connect(db) as connection:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for table in LATENT_TABLES + ("candidates_latent",):
            if table in tables:
                problems.append(f"latent table present: {table}")
        for table in tables:
            columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
            for column in columns:
                if any(fragment in column for fragment in LATENT_COLUMN_FRAGMENTS):
                    problems.append(f"latent column present: {table}.{column}")
    for name in ("candidates_eval.parquet", "submission.parquet"):
        path = participant_dir / name
        if path.is_file():
            for column in pq.read_table(path).schema.names:
                if any(fragment in column for fragment in LATENT_COLUMN_FRAGMENTS):
                    problems.append(f"latent column present: {name}.{column}")
    return problems


def competition_config(frozen: dict) -> CompetitionConfig:
    comp = frozen["competition"]
    return CompetitionConfig(
        horizon=comp["horizon"],
        max_per_user_per_day=comp["max_per_user_per_day"],
        daily_budget_per_active_user=comp["daily_budget_per_active_user"],
        daily_budget=comp["daily_budget"],
        partition_field=comp["partition_field"],
        max_training_rows=comp["max_training_rows"],
    )


def process_seed(frozen: dict, seed: int) -> dict:
    """Run the full lifecycle for one seed and return its release record.

    Never deletes the data directories itself: on Windows sqlite/pyarrow keep the
    DB locked until the process exits, so the caller (which runs this in a child
    process) deletes them afterwards.
    """
    eval_offsets = expand_offsets(frozen["competition"]["evaluation_day_offsets"])
    market_config, market_start = market_config_for(frozen, seed)
    organizer_dir = ORGANIZER_DATA_ROOT / f"seed-{seed}"
    participant_dir = PARTICIPANT_DATA_ROOT / f"seed-{seed}"
    metrics_dir = METRICS_ROOT / f"seed-{seed}"
    for path in (organizer_dir, participant_dir, metrics_dir):
        robust_rmtree(path)

    manifest = write_market_dataset(market_config, organizer_dir, market_start=market_start,
                                    output_format="sqlite")

    with sqlite3.connect(organizer_dir / "dataset.sqlite.db") as connection:
        derive_candidates(connection, manifest, eval_offsets, organizer_dir)

    baseline = build_participant_package(organizer_dir, participant_dir, frozen, seed, eval_offsets)

    problems = assert_no_latent(participant_dir)
    if problems:
        raise RuntimeError("Participant package leaked latent data: " + "; ".join(problems))

    submission = participant_dir / "submission.parquet"
    submission_sha256 = sha256(submission)
    metrics_dir.mkdir(parents=True, exist_ok=True)
    result = evaluate(
        organizer_dir, submission, metrics_dir,
        config=competition_config(frozen), population_dir=organizer_dir,
    )
    manifest_sha256 = result["data_version"]["manifest_sha256"]
    # ranked.parquet is a large generated artifact (one row per selected pair);
    # keep the small reproducible record (metrics.json / daily.csv / summary.md).
    (metrics_dir / "ranked.parquet").unlink(missing_ok=True)

    return {
        "seed": seed,
        "data_version": {"manifest_sha256": manifest_sha256,
                         "generator_version": manifest["version"]},
        "submission_sha256": submission_sha256,
        "metrics": result["metrics"],
        "evaluation_days": len(eval_offsets),
        "events": manifest["row_counts"]["events"],
        "baseline": {
            "warmup_events": baseline["warmup_events"],
            "training_status": baseline["training_status"],
            "submission_rows": baseline["submission_rows"],
            "ranking_rule": baseline["ranking_rule"],
        },
    }


def _apply_proof_overrides(frozen: dict) -> dict:
    frozen = json.loads(json.dumps(frozen))  # deep copy
    frozen["market"]["seeker_arrivals"] = 50
    frozen["market"]["job_arrivals"] = 50
    frozen["evaluation_seeds"] = [71]
    frozen["name"] = frozen["name"] + "-proof"
    return frozen


def _run_worker(config_path: Path, seed: int, proof: bool) -> dict:
    command = [sys.executable, str(Path(__file__).resolve()), "--worker", str(seed),
               "--config", str(config_path.resolve())]
    if proof:
        command.append("--proof")
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        sys.stderr.write(completed.stderr)
        raise RuntimeError(f"seed {seed} worker failed (exit {completed.returncode})")
    return json.loads(completed.stdout)


def _worker(config_path: Path, seed: int, proof: bool) -> int:
    frozen = load_frozen_config(config_path)
    if proof:
        frozen = _apply_proof_overrides(frozen)
    print(f"=== seed {seed} ===", file=sys.stderr)
    print(json.dumps(process_seed(frozen, seed), ensure_ascii=False))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_FROZEN_CONFIG)
    parser.add_argument("--seeds", type=lambda text: [int(p) for p in text.split(",")],
                        help="Comma-separated seeds (default: evaluation_seeds from the config)")
    parser.add_argument("--proof", action="store_true",
                        help="Reduced-but-real config: 50/50 arrivals, single seed 71")
    parser.add_argument("--keep-data", action="store_true",
                        help="Keep the generated DB/parquet instead of deleting them")
    parser.add_argument("--worker", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.worker is not None:
        raise SystemExit(_worker(args.config, args.worker, args.proof))

    frozen = load_frozen_config(args.config)
    proof = args.proof
    if proof:
        frozen = _apply_proof_overrides(frozen)
        print(json.dumps({"note": "proof mode: 50/50 arrivals, single seed 71; "
                                  "all other fields unchanged"}, ensure_ascii=False))
    seeds = args.seeds if args.seeds else frozen["evaluation_seeds"]

    records = []
    for seed in seeds:
        records.append(_run_worker(args.config, seed, proof))
        if not args.keep_data:
            # Delete this seed's DB/parquet before the next seed starts (storage
            # constraint: 5 production seeds must not be held simultaneously).
            robust_rmtree(ORGANIZER_DATA_ROOT / f"seed-{seed}")
            robust_rmtree(PARTICIPANT_DATA_ROOT / f"seed-{seed}")

    per_seed_primary = [r["metrics"]["macro_daily_replay_fee_yen"] for r in records]
    mean = sum(per_seed_primary) / len(per_seed_primary)
    variance = sum((value - mean) ** 2 for value in per_seed_primary) / len(per_seed_primary)
    aggregate = {
        "name": frozen["name"],
        "market": frozen["market"],
        "evaluation_seeds": seeds,
        "competition": frozen["competition"],
        "per_seed": records,
        "aggregate": {
            "macro_daily_replay_fee_yen_mean": mean,
            "macro_daily_replay_fee_yen_std": variance ** 0.5,
            "macro_daily_replay_fee_yen_per_seed": per_seed_primary,
            "total_replay_fee_yen": sum(r["metrics"]["total_replay_fee_yen"] for r in records),
        },
    }
    METRICS_ROOT.mkdir(parents=True, exist_ok=True)
    (METRICS_ROOT / "baseline_aggregate.json").write_text(
        json.dumps(aggregate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (METRICS_ROOT / "data_versions.json").write_text(
        json.dumps({
            "name": frozen["name"],
            "data_versions": {str(r["seed"]): r["data_version"]["manifest_sha256"] for r in records},
            "submission_sha256": {str(r["seed"]): r["submission_sha256"] for r in records},
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "name": frozen["name"],
        "data_versions": {str(r["seed"]): r["data_version"]["manifest_sha256"] for r in records},
        "generator_version": records[0]["data_version"]["generator_version"],
        "aggregate": aggregate["aggregate"],
        "metrics_root": str(METRICS_ROOT.resolve()),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
