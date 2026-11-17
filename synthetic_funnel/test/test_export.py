import csv
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pyarrow.parquet as pq

from synthetic_funnel import Config, LegacyScores, __version__, write_dataset
from synthetic_funnel.export import SQLITE_FILENAME, field_type
from synthetic_funnel.generator import file_hash


class ExportTest(unittest.TestCase):
    def test_formats_preserve_rows_nulls_and_manifest(self):
        config = Config(
            rows=43, seed=29, reference_rows=1000,
            base_probabilities=(0.20, 0.30, 0.25), probability_half_widths=(0.10, 0.15, 0.10),
            legacy_scores=LegacyScores(training_rows=4000),
        )
        scratch = Path("runs")
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as folder, patch("synthetic_funnel.export.BATCH_SIZE", 16):
            root = Path(folder)
            sqlite = write_dataset(config, root / "sqlite")
            parquet = write_dataset(config, root / "parquet", output_format="parquet")
            csv_manifest = write_dataset(config, root / "csv", output_format="csv")
            for name, manifest in (("sqlite", sqlite), ("parquet", parquet), ("csv", csv_manifest)):
                self.assertEqual(manifest["output_format"], name)
                self.assertEqual(manifest["version"], __version__)
                self.assertEqual(manifest["generated_expected_rates"], sqlite["generated_expected_rates"])
                self.assertEqual(manifest["generated_realized_rates"], sqlite["generated_realized_rates"])
                self.assertEqual(json.loads((root / name / "manifest.json").read_text(encoding="utf-8")), manifest)
                for file, checksum in manifest["files"].items():
                    self.assertEqual(file_hash(root / name / file), checksum)
            with closing(sqlite3.connect(root / "sqlite" / SQLITE_FILENAME)) as database:
                database.row_factory = sqlite3.Row
                self.assertEqual(database.execute("PRAGMA foreign_key_check").fetchall(), [])
                for table in ("events", "truth"):
                    rows = [dict(row) for row in database.execute(f"SELECT * FROM {table} ORDER BY event_id")]
                    expected = pq.read_table(root / "parquet" / f"{table}.parquet").to_pylist()
                    self.assertEqual(len(rows), config.rows)
                    self.assertEqual(rows, expected)
                    casts = {"TEXT": str, "INTEGER": int, "REAL": float}
                    with (root / "csv" / f"{table}.csv").open(encoding="utf-8", newline="") as stream:
                        csv_rows = [
                            {key: casts[field_type(key)](value) if value else None for key, value in row.items()}
                            for row in csv.DictReader(stream)
                        ]
                    self.assertEqual(rows, csv_rows)
                self.assertEqual(database.execute("SELECT count(legacy_decision_score) FROM events").fetchone()[0], config.rows)
                self.assertGreater(database.execute("SELECT count(*) FROM events WHERE oubo_dt IS NULL").fetchone()[0], 0)
                self.assertIsInstance(database.execute("SELECT wants_remote FROM events LIMIT 1").fetchone()[0], int)

    def test_failure_leaves_no_published_dataset(self):
        config = Config(rows=2, reference_rows=100, legacy_scores=LegacyScores(enabled=False))
        scratch = Path("runs")
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as folder:
            output = Path(folder) / "dataset"

            def fail(_rows):
                raise RuntimeError("interrupted")

            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                write_dataset(config, output, progress=fail)
            self.assertEqual(list(Path(folder).iterdir()), [])
