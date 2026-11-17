"""Command line entry point for calibration experiments."""

import argparse
import sys
from pathlib import Path

from catboost import CatBoostError
from polars.exceptions import PolarsError
from sqlalchemy.exc import SQLAlchemyError

from calibration_pipeline.pipeline import inference, train
from calibration_pipeline.source import DEFAULT_SQL_SEED, MAX_SQL_SEED


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="calibration-pipeline")
    commands = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--model-name", required=True)
    common.add_argument("--experiments-dir", type=Path, default=Path("experiments"))
    common.add_argument("--config", type=Path, required=True)
    common.add_argument("--sql", type=Path, required=True)
    common.add_argument("--sqlite", action="store_true", help="Use in-memory SQLite for local query testing")
    common.add_argument("--sqlite-fixture", type=Path, help="Copy this source DB into the SQLite run inputs")
    common.add_argument(
        "--seed", type=int, default=DEFAULT_SQL_SEED,
        help=f"SQL and model seed, from 0 to {MAX_SQL_SEED} (default: {DEFAULT_SQL_SEED})",
    )
    commands.add_parser("train", parents=[common], help="Train and evaluate a calibration model")
    scoring = commands.add_parser("inference", parents=[common], help="Score candidates with a trained model")
    scoring.add_argument("--original-score", type=Path, required=True)
    scoring.add_argument("--train-run", help="Use this successful training run instead of the latest")
    args = parser.parse_args(argv)
    try:
        common_args = (args.experiments_dir, args.model_name, args.config, args.sql)
        options = {"sqlite": args.sqlite, "seed": args.seed, "sqlite_fixture": args.sqlite_fixture}
        if args.command == "train":
            run_dir = train(*common_args, **options)
        else:
            run_dir = inference(*common_args, args.original_score, train_run=args.train_run, **options)
    except (KeyError, TypeError, ValueError, OSError, CatBoostError, PolarsError, SQLAlchemyError, ModuleNotFoundError) as error:
        print(f"calibration-pipeline: {error}", file=sys.stderr)
        return 2
    print(f"Saved calibration results: {run_dir.resolve() / 'output'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
