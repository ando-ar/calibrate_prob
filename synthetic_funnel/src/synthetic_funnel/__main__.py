import argparse
import json
import sqlite3
import sys
from pathlib import Path

from . import Config, MarketConfig, write_dataset, write_market_dataset
from .export import FORMATS


def main():
    parser = argparse.ArgumentParser(description="Generate a synthetic referral funnel dataset")
    parser.add_argument("--config", type=Path, help="JSON config; omitted keys use defaults")
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument("--run-dir", type=Path, help="Run directory containing input/config.json")
    destination.add_argument("--output", type=Path, help="New dataset directory for direct export")
    parser.add_argument("--format", choices=FORMATS, default="sqlite", help="Output format (default: sqlite)")
    parser.add_argument("--market", action="store_true", help="Generate a continuous market from a market JSON config")
    parser.add_argument('--rows', type=int, help='Override output row count')
    parser.add_argument('--progress', action='store_true', help='Report written rows on stderr')
    args = parser.parse_args()
    if args.run_dir is not None and args.config is not None:
        parser.error("--run-dir reads input/config.json; use --output with --config")
    config_path = args.run_dir / "input" / "config.json" if args.run_dir is not None else args.config
    output = args.run_dir / "output" if args.run_dir is not None else args.output
    if args.market and (config_path is None or args.rows is not None):
        parser.error("--market requires a run or config file and does not accept --rows")
    try:
        values = json.loads(config_path.read_text(encoding="utf-8-sig")) if config_path is not None else {}
        if not isinstance(values, dict):
            raise TypeError("The config must be a JSON object")
        progress = (lambda n: print(f'Written {n:,} rows', file=sys.stderr, flush=True)) if args.progress else None
        if args.market and config_path is not None:
            market_start = values.pop("market_start", None)
            legacy_models = values.pop("legacy_models", None)
            retain = values.pop("retain", "all")
            if not isinstance(market_start, str) or not isinstance(legacy_models, str) or not legacy_models:
                raise ValueError("Market config requires market_start and legacy_models as strings")
            manifest = write_market_dataset(
                MarketConfig(**values), output, market_start=market_start,
                legacy_models=config_path.parent / legacy_models, output_format=args.format, retain=retain,
                progress=progress,
            )
            row_count = manifest["row_counts"]["events"]
        else:
            if args.rows is not None:
                values['rows'] = args.rows
            manifest = write_dataset(Config.from_dict(values), output, progress=progress, output_format=args.format)
            row_count = manifest["config"]["rows"]
    except (ValueError, TypeError, OverflowError, OSError, sqlite3.Error) as exc:
        parser.exit(2, f"error: {exc}\n")
    print(json.dumps({"output": str(output.resolve()), "rows": row_count}))


if __name__ == "__main__":
    main()
