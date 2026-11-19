"""Generate the proof-size competition dataset and a participant-facing copy.

Organiser-side helper. It runs the existing ``synthetic_funnel`` generator with
the proof config in ``config.json`` and then strips the organiser-only latent
tables from the copy handed to participants:

    user_latent / item_latent / events_latent / candidates_latent are NOT distributed.

The sqlite file and parquet files are gitignored build artifacts; commit only
this script, the config and the docs.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from pathlib import Path

from synthetic_funnel import MarketConfig, write_market_dataset

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "config.json"
LATENT_TABLES = ("user_latent", "item_latent", "events_latent")


def build(config_path: Path, output_dir: Path, *, force: bool = False) -> None:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    market = dict(config["market"])
    market_start = market.pop("market_start")
    output_format = market.pop("output_format", "sqlite")
    if output_format != "sqlite":
        raise ValueError("The starter kit distributes sqlite; set market.output_format to sqlite")
    if output_dir.exists():
        if not force:
            raise FileExistsError(f"{output_dir} exists; pass --force to overwrite")
        shutil.rmtree(output_dir)
    organiser_dir = output_dir / "organiser"
    participant_dir = output_dir / "participant"

    manifest = write_market_dataset(
        MarketConfig(**market), organiser_dir, market_start=market_start, output_format="sqlite"
    )
    participant_dir.mkdir(parents=True, exist_ok=True)
    source = organiser_dir / "dataset.sqlite.db"
    target = participant_dir / "dataset.sqlite.db"
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
        for table in LATENT_TABLES:
            dst.execute(f"DROP TABLE IF EXISTS {table}")
        # The candidate-recipe indexes are created at export time in
        # synthetic_funnel.export.SQLITE_INDEXES, so the backup already has
        # them on both the organiser DB and this participant copy (issue #38).
        dst.commit()
        dst.execute("VACUUM")
    shutil.copy2(organiser_dir / "manifest.json", participant_dir / "manifest.json")
    print(json.dumps({
        "output": str(output_dir.resolve()),
        "events": manifest["row_counts"]["events"],
        "training_status": manifest["training"]["status"],
        "training_cutoff": manifest["training_cutoff"],
        "warmup_end": manifest["warmup_end"],
        "observation_end_exclusive": manifest["observation_end_exclusive"],
        "participant_tables": [
            row[0]
            for row in sqlite3.connect(target).execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ],
    }, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path, default=HERE / "data" / "proof")
    parser.add_argument("--force", action="store_true", help="Remove an existing output directory")
    args = parser.parse_args()
    build(args.config, args.output, force=args.force)


if __name__ == "__main__":
    main()
