import json
import pickle
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from xml.etree import ElementTree

import numpy as np
import polars as pl
from polars.testing import assert_frame_equal
from sqlalchemy.schema import CreateTable
from sqlalchemy_redshift.dialect import RedshiftDialect_redshift_connector

from calibration_pipeline.cli import main
from calibration_pipeline.contracts import categorical_text, integer_candidate_ids
from calibration_pipeline.evaluation import metrics
from calibration_pipeline.experiments import (
    JST,
    allocate_run,
    digest,
    select_model,
    write_json,
)
from calibration_pipeline.source import candidate_table, connect


class ExperimentTest(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.experiments = self.root / "experiments"
        self.config = self.root / "train.toml"
        self.sql = self.root / "train.sql"
        self.prediction_sql = self.root / "inference.sql"
        self.inference_config = self.root / "inference.toml"
        self.original = self.root / "original_score.csv"
        self.config.write_text("""
[model]
score = "score"
label = "label"
probability = "probability"
numeric_features = ["numeric", "empty_numeric"]
categorical_features = ["category"]
""", encoding="utf-8")
        self.inference_config.write_text("# Reserved\n", encoding="utf-8")
        self.sql.write_text("""
WITH RECURSIVE n(i) AS (
  VALUES(0) UNION ALL SELECT i+1 FROM n WHERE i<119
)
SELECT CASE WHEN i<40 THEN 'train' WHEN i<80 THEN 'calibrate' ELSE 'validation' END AS row_role,
       i*0.01 AS score,
       CASE WHEN i%3=0 THEN NULL ELSE i*0.03 END AS numeric,
       NULL AS empty_numeric,
       CASE WHEN i%3=0 THEN NULL WHEN i%3=1 THEN '0' ELSE 'x' END AS category,
       CASE WHEN i%4=0 OR i%7=0 THEN 1 ELSE 0 END AS label
FROM n;
""", encoding="utf-8")
        self.prediction_sql.write_text("""
SELECT candidate_row_id, score, NULL AS numeric, NULL AS empty_numeric,
       CASE WHEN candidate_row_id=0 THEN NULL ELSE 'unseen' END AS category
FROM calibration_candidates ORDER BY candidate_row_id DESC;
""", encoding="utf-8")
        self.frame = pl.DataFrame({
            "user_id": [10000000, 10000001], "item_id": [20000000, 20000001],
            "score": [0.2, 0.8], "other": ["first", "second"],
        })
        self.frame.write_csv(self.original)
        self.common = ["--model-name", "example_oubo", "--experiments-dir", str(self.experiments), "--sqlite"]
        self.train_args = ["train", *self.common, "--config", str(self.config), "--sql", str(self.sql)]
        self.predict_args = [
            "inference", *self.common, "--config", str(self.inference_config),
            "--sql", str(self.prediction_sql), "--original-score", str(self.original),
        ]

    def runs(self, stage):
        return sorted((self.experiments / "example_oubo" / stage).iterdir())

    def test_cli_snapshots_trains_with_nulls_and_uses_latest_or_explicit_model(self):
        self.assertEqual(main(self.train_args), 0)
        first = self.runs("train")[0]
        self.assertEqual(main(self.train_args), 0)
        second = max(self.runs("train"), key=lambda path: json.loads((path / "run_metadata.json").read_text())["started_at"])
        self.assertNotEqual(first, second)
        self.assertEqual(main(self.predict_args), 0)
        inference_run = self.runs("inference")[0]
        metadata = json.loads((inference_run / "run_metadata.json").read_text())
        self.assertEqual(metadata["train_run_id"], second.name)
        self.assertEqual(metadata["status"], "succeeded")
        self.assertEqual(metadata["input_sha256"]["original_score.csv"], digest(self.original))
        self.assertEqual((inference_run / "input" / "inference.sql").read_bytes(), self.prediction_sql.read_bytes())
        result = pl.read_csv(inference_run / "output" / "calibrated_score.csv")
        assert_frame_equal(result.select(self.frame.columns), self.frame)
        self.assertTrue(result["probability"].is_between(0, 1).all())
        report = json.loads((second / "output/calibration_metrics.json").read_text())
        self.assertEqual(report["method"], "catboost_isotonic")
        self.assertEqual(report["rows"], {"train": 40, "calibrate": 40, "validation": 40})
        ElementTree.parse(second / "output/calibration_plot.svg")
        with (first / "output/calibration_model.pkl").open("rb") as stream:
            model = pickle.load(stream)
        comparison = self.frame.with_columns(
            pl.lit(None).alias("numeric"), pl.lit(None).alias("empty_numeric"),
            pl.Series("category", [None, "unseen"]),
        )
        np.testing.assert_allclose(result["probability"].to_numpy(), model.predict(comparison))
        self.assertEqual(main([*self.predict_args, "--train-run", first.name]), 0)
        records = [json.loads((path / "run_metadata.json").read_text()) for path in self.runs("inference")]
        self.assertEqual({row["train_run_id"] for row in records}, {first.name, second.name})

    def test_numeric_categorical_codes_match_their_string_spelling(self):
        self.sql.write_text(self.sql.read_text().replace(
            "CASE WHEN i%3=0 THEN NULL WHEN i%3=1 THEN '0' ELSE 'x' END AS category",
            "CASE WHEN i%3=0 THEN NULL WHEN i%3=1 THEN 1 ELSE 2 END AS category",
        ), encoding="utf-8")
        self.prediction_sql.write_text(
            "SELECT candidate_row_id, score, NULL AS numeric, NULL AS empty_numeric, "
            "candidate_row_id * 1.0 AS category FROM calibration_candidates ORDER BY candidate_row_id DESC;",
            encoding="utf-8",
        )
        self.assertEqual(main(self.train_args), 0)
        self.assertEqual(main(self.predict_args), 0)
        inference_run = self.runs("inference")[0]
        result = pl.read_csv(inference_run / "output/calibrated_score.csv")
        self.assertTrue(result["probability"].is_between(0, 1).all())
        with (self.runs("train")[0] / "output/calibration_model.pkl").open("rb") as stream:
            model = pickle.load(stream)
        comparison = self.frame.with_columns(
            pl.lit(None).alias("numeric"), pl.lit(None).alias("empty_numeric"),
            pl.Series("category", [0.0, 1.0]),
        )
        np.testing.assert_allclose(result["probability"].to_numpy(), model.predict(comparison))

    def test_categorical_rejects_other_column_types(self):
        with self.assertRaisesRegex(ValueError, "categorical"):
            categorical_text(pl.Series([datetime(2024, 1, 1)]), "category")

    def test_score_only_always_needs_calibration_and_parquet_preserves_types(self):
        self.config.write_text('[model]\nscore="score"\n', encoding="utf-8")
        self.assertEqual(main(self.train_args), 0)
        self.original = self.original.with_suffix(".parquet")
        self.frame.write_parquet(self.original)
        args = [*self.predict_args[:-1], str(self.original)]
        self.assertEqual(main(args), 0)
        output = self.runs("inference")[0] / "output/calibrated_score.parquet"
        assert_frame_equal(pl.read_parquet(output).select(self.frame.columns), self.frame)
        self.sql.write_text(self.sql.read_text().replace("'calibrate'", "'train'"), encoding="utf-8")
        self.assertEqual(main(self.train_args), 2)
        failed = [path for path in self.runs("train") if not (path / "output").exists()]
        self.assertEqual(len(failed), 1)
        self.assertEqual(json.loads((failed[0] / "run_metadata.json").read_text())["status"], "failed")
        self.assertTrue((failed[0] / "input/train.sql").is_file())

    def test_failed_prediction_is_preserved_and_cannot_publish_outputs(self):
        self.assertEqual(main(self.train_args), 0)
        for invalid in (
            "SELECT 0 AS candidate_row_id, score, NULL AS numeric, NULL AS empty_numeric, NULL AS category FROM calibration_candidates",
            self.prediction_sql.read_text().replace("candidate_row_id, score", "candidate_row_id, score+1 AS score"),
            "SELECT * FROM missing_table",
        ):
            self.prediction_sql.write_text(invalid, encoding="utf-8")
            self.assertEqual(main(self.predict_args), 2)
        for path in self.runs("inference"):
            self.assertEqual(json.loads((path / "run_metadata.json").read_text())["status"], "failed")
            self.assertFalse((path / "output").exists())
            self.assertTrue((path / "input/inference.sql").is_file())

    def test_tampered_snapshot_fails_before_output_publication(self):
        @contextmanager
        def tamper(**options):
            path = next((self.experiments / "example_oubo/train").glob("*/input/train.sql"))
            path.write_text(path.read_text() + "\n-- modified snapshot", encoding="utf-8")
            with connect(**options) as source:
                yield source
        with patch("calibration_pipeline.pipeline.connect", tamper):
            self.assertEqual(main(self.train_args), 2)
        path = self.runs("train")[0]
        self.assertFalse((path / "output").exists())

    def test_numeric_scores_and_labels_reject_missing_or_nonfinite(self):
        original_sql = self.sql.read_text()
        for field in ("score", "label"):
            sql = original_sql
            if field == "score":
                sql = sql.replace("i*0.01 AS score", "NULL AS score")
            else:
                sql = sql.replace("THEN 1 ELSE 0 END AS label", "THEN 2 ELSE 0 END AS label")
            self.sql.write_text(sql, encoding="utf-8")
            self.assertEqual(main(self.train_args), 2)

    def test_run_allocation_collision_and_model_selection_order(self):
        start = datetime(2026, 11, 18, 12, 30, 0, tzinfo=JST)
        run_paths = [allocate_run(self.experiments, "example_oubo", "train", start) for _ in range(102)]
        self.assertEqual(run_paths[0].name, "20261118_123000")
        self.assertEqual(run_paths[100].name, "20261118_123000_100")
        for path in run_paths:
            output = path / "output"
            output.mkdir()
            artifact = output / "calibration_model.pkl"
            artifact.write_bytes(b"model")
            write_json(path / "run_metadata.json", {
                "status": "succeeded", "stage": "train", "run_id": path.name, "model_name": "example_oubo",
                "started_at": start.isoformat(), "finished_at": (start+timedelta(seconds=102-int(path.name.rsplit('_',1)[-1]) if len(path.name)>15 else 103)).isoformat(),
                "model_sha256": digest(artifact),
            })
        selected = select_model(run_paths[0].parent, None)
        self.assertEqual(selected[0], run_paths[-1].name)
        self.assertEqual(select_model(run_paths[0].parent, run_paths[0].name)[0], run_paths[0].name)
        (run_paths[-1] / "output/calibration_model.pkl").write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "hash differs"):
            select_model(run_paths[0].parent, None)

    def test_sqlite_session_table_and_redshift_ddl(self):
        frame = pl.DataFrame({"candidate_row_id": [0, 1], "score": [0.2, 0.8]})
        table = candidate_table(frame)
        dialect = RedshiftDialect_redshift_connector()
        self.assertIn("CREATE TEMPORARY TABLE calibration_candidates", str(CreateTable(table).compile(dialect=dialect)))
        self.assertIn("), (", str(table.insert().values(frame.to_dicts()).compile(dialect=dialect)))
        with connect(sqlite=True) as source:
            source.upload(frame)
            self.assertEqual(source.execute_sql("SELECT count(*) AS n FROM sqlite_temp_master WHERE name='calibration_candidates'")["n"][0], 1)
            self.assertEqual(source.execute_sql("SELECT count(*) AS n FROM main.sqlite_master WHERE name='calibration_candidates'")["n"][0], 0)
        with connect(sqlite=True) as source:
            self.assertEqual(source.execute_sql("SELECT count(*) AS n FROM sqlite_temp_master WHERE name='calibration_candidates'")["n"][0], 0)

    def test_ids_and_ece(self):
        with self.assertRaisesRegex(ValueError, "8-digit integer"):
            integer_candidate_ids(pl.DataFrame({"user_id": [10_000_000.0], "item_id": [20_000_000]}), csv_input=False)
        report = metrics(np.array([0, 1, 1]), np.array([0.0, 0.4, 1.0]), bins=5)
        self.assertAlmostEqual(report["ece"], 0.2)

    def test_seed_reproducible_sampling(self):
        def sampled(seed):
            with connect(sqlite=True) as source:
                source.set_seed(seed)
                return source.execute_sql(
                    "WITH RECURSIVE ids(id) AS (VALUES(1) UNION ALL SELECT id+1 FROM ids WHERE id<100) "
                    "SELECT id FROM ids ORDER BY random() LIMIT 10"
                )["id"].to_list()
        self.assertEqual(sampled(123), sampled(123))
        self.assertNotEqual(sampled(123), sampled(456))
