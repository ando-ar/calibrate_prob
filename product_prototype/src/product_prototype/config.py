"""Explicit experiment configuration. All paths are relative to the TOML file."""
from __future__ import annotations

import re
import math
import tomllib
from dataclasses import dataclass
from pathlib import Path

import pandas as pd


def identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError(f"Invalid SQL identifier: {value!r}")
    return '"' + value + '"'


def table_identifier(value: str) -> str:
    parts = value.split(".")
    if not 1 <= len(parts) <= 3:
        raise ValueError("Use table, schema.table, or database.schema.table")
    return ".".join(identifier(part) for part in parts)


def timestamp(value: str, timezone: str = "UTC") -> pd.Timestamp:
    result = pd.Timestamp(value)
    if result.tzinfo is None:
        result = result.tz_localize(timezone)
    return result.tz_convert("UTC").tz_localize(None)


@dataclass(frozen=True)
class Config:
    path: Path
    values: dict

    def section(self, key: str) -> dict:
        return self.values[key]

    def resolve(self, value: str) -> Path:
        return (self.path.parent / value).resolve()

    @property
    def output(self) -> Path:
        return self.resolve(self.values["experiment"]["output_dir"])

    @property
    def columns(self) -> list[str]:
        source = self.values["source"]
        cols = [source[key] for key in ("user_column", "item_column", "anchor_column")]
        if source.get("row_id_column"):
            cols.append(source["row_id_column"])
        labels = self.values["labels"]
        cols.extend(labels[key] for key in ("application_column", "screen_column", "target_column"))
        if labels.get("fee_column"):
            cols.append(labels["fee_column"])
        cols.extend(self.values["features"]["numeric"])
        cols.extend(self.values["features"]["categorical"])
        for proxy in self.values["proxies"]:
            cols.extend(proxy[key] for key in ("lower_column", "upper_column", "desired_column", "rate_column")
                        if proxy.get(key))
        for dataset in self.values["datasets"]:
            cols.extend(c for c in dataset.get("balance_columns", []) if c != "_month")
        return list(dict.fromkeys(cols))

    def date(self, section: str, key: str) -> pd.Timestamp:
        return timestamp(self.values[section][key], self.values["source"].get("timezone", "UTC"))


def load_config(path: Path) -> Config:
    path = path.resolve()
    with path.open("rb") as source:
        values = tomllib.load(source)
    config = Config(path, values)
    if values["experiment"].get("feature_snapshot_mode") != "latest":
        raise ValueError("This prototype explicitly records feature_snapshot_mode = 'latest'")
    if values["source"]["kind"] not in ("file", "redshift"):
        raise ValueError("source.kind must be file or redshift")
    for column in config.columns:
        identifier(column)
        if column.startswith("_"):
            raise ValueError("Source columns starting with '_' are reserved")
    if values["source"]["kind"] == "redshift":
        table_identifier(values["source"]["table"])
    starts = [config.date("split", key) for key in ("train_start", "train_end", "validation_end", "test_end")]
    if any(a >= b for a, b in zip(starts, starts[1:])):
        raise ValueError("Split boundaries must be strictly increasing; all ranges are half-open")
    horizon = values["labels"]["horizon_days"]
    if type(horizon) is not int or horizon <= 0:
        raise ValueError("labels.horizon_days must be a positive integer")
    if starts[-1] + pd.Timedelta(days=horizon) > config.date("labels", "as_of"):
        raise ValueError("test_end + horizon_days must be <= as_of so every test label has matured")
    if values["split"].get("purge_label_overlap", True) and any(
        end - pd.Timedelta(days=horizon) <= start for start, end in zip(starts[:2], starts[1:3])
    ):
        raise ValueError("Train and validation intervals must exceed horizon_days when purging label overlap")
    if values["ranking"]["max_per_user_per_day"] <= 0:
        raise ValueError("max_per_user_per_day must be positive")
    ranking = values["ranking"]
    for key in ("max_per_user_per_day", "daily_budget", "support_user_top_n", "support_top_k"):
        if key in ranking and (type(ranking[key]) is not int or ranking[key] <= 0):
            raise ValueError(f"ranking.{key} must be a positive integer")
    if "daily_budget_per_active_user" in ranking and not 0 < ranking["daily_budget_per_active_user"] < float("inf"):
        raise ValueError("daily_budget_per_active_user must be positive and finite")
    if not 0 < values["source"].get("chunk_rows", 50000):
        raise ValueError("chunk_rows must be positive")
    for key in ("datasets", "models", "proxies", "scorers"):
        names = [item["name"] for item in values[key]]
        if not names or len(set(names)) != len(names) or any(not re.fullmatch(r"[A-Za-z0-9_-]+", n) or "__" in n for n in names):
            raise ValueError(f"{key} names must be distinct safe filenames")
    numeric, categorical = values["features"]["numeric"], values["features"]["categorical"]
    if not numeric and not categorical:
        raise ValueError("At least one model feature is required")
    if set(numeric) & set(categorical) or len(set(numeric + categorical)) != len(numeric + categorical):
        raise ValueError("Feature columns must be distinct")
    forbidden = {values["labels"][key] for key in ("application_column", "screen_column", "target_column")}
    forbidden.update(values["source"][key] for key in ("user_column", "item_column", "anchor_column"))
    if values["source"].get("row_id_column"):
        forbidden.add(values["source"]["row_id_column"])
    if values["labels"].get("fee_column"):
        forbidden.add(values["labels"]["fee_column"])
    if forbidden & set(numeric + categorical):
        raise ValueError("Identifiers, event dates and actual fees cannot be direct model features")
    partitions = ranking.get("partition_columns", [])
    if len(set(partitions)) != len(partitions) or not set(partitions) <= set(categorical):
        raise ValueError("ranking.partition_columns must be distinct categorical features")
    for dataset in values["datasets"]:
        if dataset["mode"] not in ("uniform", "stage") or not dataset.get("rationale", "").strip():
            raise ValueError("Each dataset requires mode=uniform/stage and a rationale")
        if dataset.get("max_rows", 1) <= 0 or dataset.get("months", 1) <= 0:
            raise ValueError("Dataset limits and months must be positive when supplied")
        if any(type(dataset[key]) is not int for key in ("max_rows", "months") if key in dataset):
            raise ValueError("max_rows and months must be integers")
        if dataset["mode"] == "stage":
            shares = dataset["negative_shares"]
            if set(shares) != {"screen", "apply", "no_apply"} or any(not math.isfinite(v) or v < 0 for v in shares.values()):
                raise ValueError("negative_shares requires screen, apply and no_apply")
            if abs(sum(shares.values()) - 1) > 1e-8:
                raise ValueError("negative_shares must sum to one")
        balance = set(dataset.get("balance_columns", [])) - {"_month"}
        if not balance <= set(categorical):
            raise ValueError("balance_columns must be categorical features or _month")
    proxy_names = {p["name"] for p in values["proxies"]}
    if values["evaluation"]["proxy"] not in proxy_names:
        raise ValueError("evaluation.proxy must name one common configured proxy")
    if values["evaluation"]["selection_metric"] == "macro_daily_observed_fee_yen" and not values["labels"].get("fee_column"):
        raise ValueError("Observed-fee selection requires labels.fee_column")
    if values["evaluation"]["selection_metric"] in ("log_loss", "brier") and values["evaluation"].get("higher_is_better", True):
        raise ValueError("Set higher_is_better=false for probability-error metrics")
    for proxy in values["proxies"]:
        if proxy["kind"] not in ("constant", "salary_midpoint", "delta_salary"):
            raise ValueError("Unsupported proxy kind")
        if not proxy.get("rationale", "").strip():
            raise ValueError("Each proxy requires a rationale for its numerical assumptions")
        for key in ("fallback_fee_yen", "fee_rate", "salary_unit_yen", "desired_weight"):
            if key in proxy and (not math.isfinite(proxy[key]) or proxy[key] < 0):
                raise ValueError(f"proxy.{key} must be nonnegative and finite")
        if "fallback_fee_yen" not in proxy:
            raise ValueError("Each proxy needs an explicit fallback_fee_yen")
    for model in values["models"]:
        if model["kind"] not in ("logistic", "hist_gradient_boosting"):
            raise ValueError("Unsupported model kind")
    for scorer in values["scorers"]:
        if scorer["kind"] not in ("probability", "probability_times_proxy", "odds_times_proxy", "two_stage_fee"):
            raise ValueError("Unsupported scorer kind")
        if scorer.get("proxy") not in proxy_names:
            raise ValueError("Every scorer must name a configured proxy, also used for proxy-label metrics")
        if scorer["kind"] == "two_stage_fee" and not values["labels"].get("fee_column"):
            raise ValueError("two_stage_fee needs an observed fee column")
        if scorer.get("fee_dataset") and scorer["fee_dataset"] not in {d["name"] for d in values["datasets"]}:
            raise ValueError("fee_dataset must name a configured dataset")
        if "fee_max_rows" in scorer and (type(scorer["fee_max_rows"]) is not int or scorer["fee_max_rows"] < 2 or not scorer.get("fee_rationale", "").strip()):
            raise ValueError("fee_max_rows must be >= 2 with a fee_rationale")
    return config
