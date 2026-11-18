import argparse
import json
import sqlite3
import sys
from pathlib import Path

from . import MarketConfig, write_market_dataset
from .export import FORMATS

REMOVED_KEYS = ("rows", "legacy_models", "retain", "legacy_scores", "referral_start",
                "referral_end", "as_of")


def main():
    parser = argparse.ArgumentParser(description="Generate a synthetic continuous-market referral dataset")
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument("--run-dir", type=Path, help="Run directory containing input/config.json")
    destination.add_argument("--output", type=Path, help="New dataset directory for direct export")
    parser.add_argument("--config", type=Path, help="JSON config; omitted keys use defaults (use with --output)")
    parser.add_argument("--format", choices=FORMATS, default="sqlite", help="Output format (default: sqlite)")
    parser.add_argument("--seeker-arrivals", type=int, help="Override daily seeker arrivals from the config")
    parser.add_argument("--job-arrivals", type=int, help="Override daily job arrivals from the config")
    parser.add_argument("--progress", action="store_true", help="Report written rows on stderr")
    args = parser.parse_args()
    if args.run_dir is not None and args.config is not None:
        parser.error("--run-dir reads input/config.json; use --output with --config")
    config_path = args.run_dir / "input" / "config.json" if args.run_dir is not None else args.config
    output = args.run_dir / "output" if args.run_dir is not None else args.output
    if config_path is None:
        parser.error("market generation requires a config file: use --run-dir or --config")
    try:
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
        for key, value in (("seeker_arrivals", args.seeker_arrivals), ("job_arrivals", args.job_arrivals)):
            if value is not None:
                values[key] = value
        progress = (lambda n: print(f"Written {n:,} rows", file=sys.stderr, flush=True)) if args.progress else None
        manifest = write_market_dataset(
            MarketConfig(**values), output, market_start=market_start,
            output_format=args.format, progress=progress,
        )
    except (ValueError, TypeError, OverflowError, OSError, sqlite3.Error) as exc:
        parser.exit(2, f"error: {exc}\n")
    print(json.dumps({"output": str(output.resolve()), "rows": manifest["row_counts"]["events"]}))


if __name__ == "__main__":
    main()
