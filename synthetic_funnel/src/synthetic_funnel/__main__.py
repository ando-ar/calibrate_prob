import argparse
import json
import sqlite3
import sys
from pathlib import Path

from . import MarketConfig, write_market_dataset
from .export import FORMATS
from .seed_batch import generate_seeds

REMOVED_KEYS = ("rows", "legacy_models", "retain", "legacy_scores", "referral_start",
                "referral_end", "as_of")


def _load_config(config_path):
    values = json.loads(config_path.read_text(encoding="utf-8-sig"))
    if not isinstance(values, dict):
        raise TypeError("The config must be a JSON object")
    removed = [key for key in REMOVED_KEYS if key in values]
    if removed:
        raise ValueError("Removed config keys: " + ", ".join(sorted(removed)) +
                         "; market generation uses only MarketConfig keys and market_start")
    market_start = values.pop("market_start", None)
    if not isinstance(market_start, str) or not market_start:
        raise ValueError("Market config requires market_start as a string")
    return values, market_start


def _seeds_argument(text):
    try:
        seeds = tuple(int(part.strip()) for part in text.split(",") if part.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--seeds must be comma-separated integers") from exc
    if not seeds:
        raise argparse.ArgumentTypeError("--seeds must not be empty")
    if len(set(seeds)) != len(seeds):
        raise argparse.ArgumentTypeError("--seeds must not contain duplicates")
    return seeds


def main():
    parser = argparse.ArgumentParser(description="Generate a synthetic continuous-market referral dataset")
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument("--run-dir", type=Path, help="Run directory containing input/config.json")
    destination.add_argument("--output", type=Path, help="New dataset directory for direct export")
    parser.add_argument("--config", type=Path, help="JSON config; omitted keys use defaults (use with --output)")
    parser.add_argument("--format", choices=FORMATS, default="sqlite", help="Output format (default: sqlite)")
    parser.add_argument("--seed", type=int, help="Override the seed from the config (single-seed run)")
    parser.add_argument("--seeds", type=_seeds_argument,
                        help="Generate these seeds concurrently into seed-<n> subdirectories")
    parser.add_argument("--jobs", type=int, help="Maximum concurrent seed processes (default: CPU count)")
    parser.add_argument("--seeker-arrivals", type=int, help="Override daily seeker arrivals from the config")
    parser.add_argument("--job-arrivals", type=int, help="Override daily job arrivals from the config")
    parser.add_argument("--progress", action="store_true", help="Report written rows on stderr")
    args = parser.parse_args()
    if args.run_dir is not None and args.config is not None:
        parser.error("--run-dir reads input/config.json; use --output with --config")
    if args.seeds is not None and args.seed is not None:
        parser.error("--seed and --seeds are mutually exclusive")
    if args.jobs is not None and args.seeds is None:
        parser.error("--jobs requires --seeds")
    config_path = args.run_dir / "input" / "config.json" if args.run_dir is not None else args.config
    output = args.run_dir / "output" if args.run_dir is not None else args.output
    if config_path is None:
        parser.error("market generation requires a config file: use --run-dir or --config")
    try:
        values, market_start = _load_config(config_path)
        for key, value in (("seeker_arrivals", args.seeker_arrivals), ("job_arrivals", args.job_arrivals)):
            if value is not None:
                values[key] = value
        if args.seeds is not None:
            summary = generate_seeds(
                config_path, output, args.seeds, jobs=args.jobs, output_format=args.format,
                seeker_arrivals=args.seeker_arrivals, job_arrivals=args.job_arrivals,
                progress=args.progress,
            )
        else:
            if args.seed is not None:
                values["seed"] = args.seed
            progress = (lambda n: print(f"Written {n:,} rows", file=sys.stderr, flush=True)) if args.progress else None
            manifest = write_market_dataset(
                MarketConfig(**values), output, market_start=market_start,
                output_format=args.format, progress=progress,
            )
            summary = {"output": str(output.resolve()), "rows": manifest["row_counts"]["events"]}
    except (ValueError, TypeError, OverflowError, OSError, sqlite3.Error, RuntimeError) as exc:
        parser.exit(2, f"error: {exc}\n")
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
