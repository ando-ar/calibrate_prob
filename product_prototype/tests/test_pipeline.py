from __future__ import annotations

from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import pytest

from product_prototype.config import load_config, identifier
from product_prototype.dataset import Catalog, allocate
from product_prototype.demo import generate
from product_prototype.io import read_json
from product_prototype.model import fit_model, proxy_fee
from product_prototype.ranking import rank, support_tables
from product_prototype.runner import run
from product_prototype.source import extraction_sql, normalize, snapshot
from product_prototype.source import source_batches
from product_prototype.runner import output_lock


@pytest.fixture
def config(tmp_path):
    example = Path(__file__).parents[1] / "configs" / "example.toml"
    content = example.read_text(encoding="utf-8").replace("../data/demo.parquet", "data.parquet").replace(
        "../runs/demo", "runs").replace("max_rows = 300000", "max_rows = 1500")
    path = tmp_path / "config.toml"
    path.write_text(content, encoding="utf-8")
    generate(tmp_path / "data.parquet", rows=10000)
    return load_config(path)


def test_boundaries_and_extraction(config):
    sql, params = extraction_sql(config)
    assert '"shoukai_dt" >= %s' in sql and '"shoukai_dt" < %s' in sql
    assert len(params) == 2 and params[0].year == 2023
    assert "password" not in sql
    with pytest.raises(ValueError):
        identifier('x; DROP TABLE history')
    path = snapshot(config)
    original = path.stat().st_mtime_ns
    assert snapshot(config) == path and path.stat().st_mtime_ns == original
    manifest = read_json(path.parent / "manifest.json")
    assert manifest["latest_feature_leakage_accepted"]


def test_mature_labels_purge_and_stage_priority(config, tmp_path):
    raw = pd.read_parquet(config.resolve(config.section("source")["path"])).iloc[:5].copy()
    raw["shoukai_dt"] = pd.Timestamp("2024-01-01")
    raw["oubo_dt"] = pd.Timestamp("2024-01-03")
    raw["shorui_tsuuka_dt"] = pd.Timestamp("2024-01-15")
    raw["kettei_dt"] = [pd.Timestamp("2024-04-29"), pd.Timestamp("2024-04-30"),
                         pd.Timestamp("2023-12-31"), pd.NaT, pd.Timestamp("2024-02-01")]
    # 120 days after Jan 1 in a leap year is Apr 30: upper bound is exclusive.
    raw.loc[raw.index[3], "shorui_tsuuka_dt"] = pd.NaT
    normalized = normalize(raw, config)
    path = tmp_path / "labels.parquet"
    normalized.to_parquet(path, index=False)
    with Catalog(config, path) as catalog:
        rows = catalog.db.sql("SELECT _label, _stage FROM examples ORDER BY event_id").fetchall()
        assert rows == [(1, "positive"), (0, "screen"), (0, "screen"), (0, "apply"), (1, "positive")]
        assert catalog.interval("train")[1] + pd.Timedelta(days=120) == config.date("split", "train_end")
        assert catalog.interval("validation")[1] + pd.Timedelta(days=120) == config.date("split", "validation_end")


def test_deterministic_sampling_and_backfill(config, tmp_path):
    with Catalog(config, snapshot(config)) as catalog:
        plan = config.values["datasets"][1]
        a = catalog.sample(plan, tmp_path / "a")
        b = catalog.sample(plan, tmp_path / "b")
        assert a["_row_id"].tolist() == b["_row_id"].tolist()
        assert len(a) == 1500 and a["_row_id"].is_unique
        assert ((a["_sampling_probability"] > 0) & (a["_sampling_probability"] <= 1)).all()
        query, params = catalog.split_query("train")
        positives = catalog.db.execute(f"SELECT count(*) FROM ({query}) WHERE _label=1", params).fetchone()[0]
        assert a["_label"].sum() == positives
        assert (a.loc[a["_label"] == 1, "_sampling_probability"] == 1).all()
        balanced = catalog.sample(config.values["datasets"][2], tmp_path / "balanced")
        assert len(balanced) == 1500 and balanced["_stratum"].nunique() > 2
    assert allocate({"a": 1, "b": 100, "c": 100}, 11) == {"a": 1, "b": 5, "c": 5}


def test_proxy_clip_units_and_missing():
    proxy = {"kind": "delta_salary", "fallback_fee_yen": 2000000, "lower_column": "l",
             "upper_column": "u", "desired_column": "a", "desired_weight": 0.5,
             "salary_unit_yen": 10000, "fee_rate": 0.3}
    values = proxy_fee(pd.DataFrame({"l": [600, 600, np.nan], "u": [900, 900, np.nan],
                                   "a": [700, 1000, np.nan]}), proxy)
    np.testing.assert_allclose(values, [725 * 10000 * .3, 825 * 10000 * .3, 2000000])


def test_daily_cap_dedup_ties_and_support():
    frame = pd.DataFrame({"_row_id": ["a", "b", "c", "d", "e", "f"], "_day": pd.to_datetime(
        ["2026-01-01"] * 5 + ["2026-01-02"]), "_user": ["u", "u", "u", "v", "v", "u"],
        "_item": ["i", "i", "j", "i", "k", "j"], "score": [10., 9., 8., 8., 7., 1.]})
    ranked = rank(frame, {"max_per_user_per_day": 1, "daily_budget": 2})
    assert set(ranked.loc[ranked["selected"], "_row_id"]) == {"a", "d", "f"}
    user, item = support_tables(frame, {})
    assert user.loc[(user["_user"] == "u") & (user["_day"] == pd.Timestamp("2026-01-01")), "score"].item() == 18
    assert item["score"].max() == 10


@pytest.mark.parametrize("kind", ["logistic", "hist_gradient_boosting"])
def test_model_bundle_with_unseen_categories(config, tmp_path, kind):
    with Catalog(config, snapshot(config)) as catalog:
        data = catalog.sample(config.values["datasets"][1], tmp_path / "training")
    specification = {"kind": kind, "parameters": {"max_iter": 1000 if kind == "logistic" else 10}}
    model = fit_model(data, config.section("features"), specification, config.values["scorers"][3],
                      config.values["proxies"][1], "fee_yen", 42)
    x = data.head().copy()
    x["market"] = "never_seen"
    before = model.predict(x)
    assert np.isfinite(before["score"]).all()
    path = tmp_path / "model.joblib"
    joblib.dump(model, path)
    pd.testing.assert_frame_equal(before, joblib.load(path).predict(x))
    assert model.metadata["fee_training_rows"] > 0
    assert model.metadata["component_training_rows"] == len(data) + model.metadata["fee_training_rows"]


def test_run_resume_and_frozen_test(config):
    # Real extraction, sampling, fitting, evaluation and serialization; two competing scorers.
    config.values["datasets"] = config.values["datasets"][:1]
    config.values["models"] = config.values["models"][:1]
    config.values["scorers"] = config.values["scorers"][:2]
    config.values["ranking"]["daily_budget"] = 2
    first = run(config)
    checkpoint = config.output / "test.json"
    mtime = checkpoint.stat().st_mtime_ns
    second = run(config)
    assert first == second and checkpoint.stat().st_mtime_ns == mtime
    assert len(list((config.output / "test").glob("ranked.parquet"))) == 1
    assert len(list(config.output.glob("candidates/*/validation.json"))) == 2
    with pytest.raises(ValueError, match="Cannot retry"):
        run(config, retry_failed=True)
    config.values["models"][0]["parameters"]["C"] = 2
    with pytest.raises(ValueError, match="changed"):
        run(config)


def test_censored_rows_are_not_negative(config, tmp_path):
    raw = pd.read_parquet(config.resolve(config.section("source")["path"])).head(1).copy()
    raw["shoukai_dt"] = pd.Timestamp("2026-10-25")
    raw["kettei_dt"] = pd.NaT
    path = tmp_path / "recent.parquet"
    normalize(raw, config).to_parquet(path, index=False)
    with Catalog(config, path) as catalog:
        assert catalog.db.sql("SELECT count(*) FROM examples").fetchone()[0] == 0


def test_user_cap_is_shared_across_markets():
    frame = pd.DataFrame({"_row_id": ["a", "b", "c"], "_day": pd.to_datetime(["2026-01-01"] * 3),
                          "_user": ["u", "u", "v"], "_item": ["i", "j", "k"],
                          "score": [3., 2., 1.], "market": ["x", "y", "y"]})
    ranked = rank(frame, {"max_per_user_per_day": 1, "partition_columns": ["market"],
                          "daily_budget_per_active_user": 1})
    assert set(ranked.loc[ranked["selected"], "_row_id"]) == {"a", "c"}


def test_fee_count_changes_without_changing_classifier(config, tmp_path):
    with Catalog(config, snapshot(config)) as catalog:
        data = catalog.sample(config.values["datasets"][1], tmp_path / "sample")
    scorer = dict(config.values["scorers"][3])
    model = fit_model(data, config.section("features"), config.values["models"][0], scorer,
                      config.values["proxies"][1], "fee_yen", 42)
    scorer["fee_max_rows"] = 25
    limited = fit_model(data, config.section("features"), config.values["models"][0], scorer,
                        config.values["proxies"][1], "fee_yen", 42, data)
    np.testing.assert_array_equal(model.classifier.named_steps["model"].coef_, limited.classifier.named_steps["model"].coef_)
    assert limited.metadata["fee_training_rows"] == 25 < model.metadata["fee_training_rows"]


def test_output_lock(config):
    with output_lock(config.output):
        with pytest.raises(RuntimeError, match="Another process"):
            with output_lock(config.output):
                pass
    with output_lock(config.output):
        pass


def test_redshift_readonly_adapter(config, monkeypatch):
    import sys
    import types
    from unittest.mock import MagicMock

    connection, cursor = MagicMock(), MagicMock()
    connection.cursor.return_value = cursor
    cursor.description = [("user_id",), ("item_id",)]
    cursor.fetchmany.side_effect = [[("001", "002")], []]
    connect = MagicMock(return_value=connection)
    monkeypatch.setitem(sys.modules, "redshift_connector", types.SimpleNamespace(connect=connect))
    for name in ("HOST", "DATABASE", "USER", "PASSWORD"):
        monkeypatch.setenv("REDSHIFT_" + name, "test-only-not-real")
    config.values["source"]["kind"] = "redshift"
    batches = list(source_batches(config))
    assert batches[0]["user_id"].iloc[0] == "001"
    assert connect.call_args.kwargs["sslmode"] == "verify-full"
    sql, params = cursor.execute.call_args.args
    assert sql.startswith("SELECT ") and len(params) == 2
    assert cursor.paramstyle == "format"
    connection.rollback.assert_called_once()
    connection.close.assert_called_once()
    cursor.close.assert_called_once()
