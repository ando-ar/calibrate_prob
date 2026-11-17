"""CLI entry points; paths passed as arguments are relative to the working directory."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import joblib
import pandas as pd

from .config import load_config
from .demo import generate
from .io import write_json, file_hash
from .ranking import rank, support_tables
from .runner import output_lock, run
from .source import extraction_sql, normalize, snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="Generate deterministic synthetic input (no database access)")
    demo.add_argument("--output", type=Path, default=Path("data/demo.parquet"))
    demo.add_argument("--rows", type=int, default=18000)
    for name in ("sql", "extract", "run", "predict"):
        command = commands.add_parser(name)
        command.add_argument("--config", type=Path, required=True)
        if name == "run":
            command.add_argument("--retry-failed", action="store_true")
        if name == "predict":
            command.add_argument("--model", type=Path, required=True)
            command.add_argument("--input", type=Path, required=True)
            command.add_argument("--output", type=Path, required=True, help="New directory for ranked candidates and support scores")
    args = parser.parse_args()
    if args.command == "demo":
        result = generate(args.output, args.rows)
    else:
        config = load_config(args.config)
        if args.command == "sql":
            sql, params = extraction_sql(config)
            result = {"sql": sql, "parameters": [value.isoformat() for value in params]}
        elif args.command == "extract":
            with output_lock(config.output):
                result = {"snapshot": str(snapshot(config))}
        elif args.command == "run":
            result = run(config, retry_failed=args.retry_failed)
        else:
            if args.output.exists():
                raise FileExistsError("Choose a new inference output directory")
            model = joblib.load(args.model)
            raw = pd.read_csv(args.input, dtype="string") if args.input.suffix.lower() == ".csv" else pd.read_parquet(args.input)
            frame = normalize(raw, config, inference=True)
            predictions = model.predict(frame)
            base = frame[["_row_id", "_day", "_user", "_item"] + config.section("ranking").get("partition_columns", [])].reset_index(drop=True)
            scored = pd.concat([base, predictions.reset_index(drop=True)], axis=1)
            ranked = rank(scored, config.section("ranking"))
            args.output.mkdir(parents=True)
            ranked.to_parquet(args.output / "ranked.parquet", index=False)
            for name, support in zip(("user", "item"), support_tables(scored, config.section("ranking"))):
                support.to_parquet(args.output / f"support_{name}.parquet", index=False)
            result = {"rows": len(ranked), "selected_rows": int(ranked["selected"].sum()),
                      "model": model.metadata, "model_sha256": file_hash(args.model),
                      "input_sha256": file_hash(args.input), "ranking": config.section("ranking")}
            write_json(args.output / "manifest.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
