"""Daily constrained ranking and explicitly observational offline metrics."""
from __future__ import annotations

from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, brier_score_loss, log_loss, roc_auc_score

from .config import identifier


def rank(frame: pd.DataFrame, settings: dict) -> pd.DataFrame:
    """Keep each user/item/day once, enforce individual cap, then global budget."""
    if not np.isfinite(frame["score"].to_numpy(dtype=float)).all():
        raise ValueError("Ranking requires finite scores")
    if not frame["_row_id"].is_unique:
        raise ValueError("Ranking requires unique row IDs")
    groups = ["_day"] + settings.get("partition_columns", [])
    if frame[groups + ["_user", "_item", "_row_id"]].isna().any().any():
        raise ValueError("Ranking keys and partition columns cannot be NULL")
    group = ", ".join(identifier(c) for c in groups)
    cap = int(settings["max_per_user_per_day"])
    budget = "TRUE"
    if "daily_budget_per_active_user" in settings:
        budget = f"daily_rank <= floor(active_users * {float(settings['daily_budget_per_active_user'])})"
    if "daily_budget" in settings:
        budget += f" AND daily_rank <= {int(settings['daily_budget'])}"
    # A user cap is shared across partitions, so one person cannot receive cap per market.
    with duckdb.connect() as db:
        db.register("input_rows", frame)
        query = f"""
            WITH unique_pairs AS (
                SELECT *, row_number() OVER (PARTITION BY _day, _user, _item
                    ORDER BY score DESC, _row_id) AS pair_rank FROM input_rows
            ), user_ranked AS (
                SELECT *, row_number() OVER (PARTITION BY _day, _user
                    ORDER BY score DESC, _row_id) AS user_rank
                FROM unique_pairs WHERE pair_rank = 1
            ), eligible AS (
                SELECT * FROM user_ranked WHERE user_rank <= {cap}
            ), ranked AS (
                SELECT *, row_number() OVER (PARTITION BY {group}
                    ORDER BY score DESC, _row_id) AS daily_rank FROM eligible
            ), populations AS (
                SELECT {group}, count(DISTINCT _user) AS active_users
                FROM input_rows GROUP BY {group}
            ), chosen AS (
                SELECT _row_id, user_rank, daily_rank, ({budget}) AS selected
                FROM ranked JOIN populations USING ({group})
            )
            SELECT input_rows.*, chosen.user_rank, chosen.daily_rank,
                   coalesce(chosen.selected, FALSE) AS selected
            FROM input_rows LEFT JOIN chosen USING (_row_id)
            ORDER BY _day, score DESC, _row_id
        """
        return db.sql(query).df()


def support_tables(frame: pd.DataFrame, settings: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Legacy support aggregates: user top-five sum and item maximum, before caps."""
    unique = frame.sort_values(["score", "_row_id"], ascending=[False, True]).drop_duplicates(
        ["_day", "_user", "_item"])
    top_n = settings.get("support_user_top_n", 5)
    user = unique.groupby(["_day", "_user"], sort=False).head(top_n).groupby(
        ["_day", "_user"], as_index=False)["score"].sum()
    item = unique.groupby(["_day", "_item"], as_index=False)["score"].max()
    return user, item


def evaluate(frame: pd.DataFrame, settings: dict, folder: Path) -> dict:
    folder.mkdir(parents=True, exist_ok=True)
    ranked = rank(frame, settings)
    ranked.to_parquet(folder / "ranked.parquet", index=False)
    y, p = frame["_label"].to_numpy(), frame["probability"].to_numpy()
    selected = ranked[ranked["selected"]]
    daily = ranked.groupby(["_day"] + settings.get("partition_columns", []), as_index=False).agg(
        candidates=("_row_id", "size"), selected=("selected", "sum"))
    selected = selected.copy()
    selected["observed_positive_proxy_yen"] = selected["_label"] * selected["evaluation_proxy_fee"]
    selected["observed_fee_yen"] = selected["_label"] * selected["_observed_fee"].fillna(0)
    keys = ["_day"] + settings.get("partition_columns", [])
    totals = selected.groupby(keys, as_index=False).agg(
        observed_positives=("_label", "sum"), observed_fee_yen=("observed_fee_yen", "sum"),
        observed_positive_proxy_yen=("observed_positive_proxy_yen", "sum"))
    daily = daily.merge(totals, on=keys, how="left").fillna(0)
    daily.to_csv(folder / "daily.csv", index=False)
    partitions = settings.get("partition_columns", [])
    # First average over days per market, then over markets (legacy equal-market evaluation).
    macro = daily.groupby(partitions)[["observed_fee_yen", "observed_positive_proxy_yen", "observed_positives"]].mean().mean() if partitions else daily[["observed_fee_yen", "observed_positive_proxy_yen", "observed_positives"]].mean()
    result = {"rows": len(frame), "positive_rows": int(y.sum()), "selected_rows": len(selected),
              "roc_auc": float(roc_auc_score(y, p)) if len(np.unique(y)) == 2 else None,
              "average_precision": float(average_precision_score(y, p)) if y.sum() else None,
              "log_loss": float(log_loss(y, p, labels=[0, 1])),
              "brier": float(brier_score_loss(y, p)),
              "observed_selected_positives": int(selected["_label"].sum()),
              "observed_positive_capture": float(selected["_label"].sum() / y.sum()) if y.sum() else None,
              "observed_fee_missing_positive_rows": int(((frame["_label"] == 1) & frame["_observed_fee"].isna()).sum()),
              "latest_feature_leakage_accepted": True,
              "evaluation_scope": "logged_cohort_only_not_counterfactual_revenue"}
    result.update({"macro_daily_" + key: float(value) for key, value in macro.items()})
    for entity, support in zip(("user", "item"), support_tables(frame, settings)):
        column = "_" + entity
        positives = frame.groupby(["_day", column], as_index=False)["_label"].max()
        support = support.merge(positives, on=["_day", column], how="left")
        support = support.sort_values(["_day", "score", column], ascending=[True, False, True])
        support["rank"] = support.groupby("_day").cumcount() + 1
        top = support[support["rank"] <= settings.get("support_top_k", 10000)]
        result[f"observed_{entity}_precision_at_k"] = float(top.groupby("_day")["_label"].mean().mean())
        support.to_parquet(folder / f"support_{entity}.parquet", index=False)
    return result
