"""issue #46: before/after selection-effect experiment (proof scale, TEMP data).

Demonstrates that, once occupation/region segment effects and temporal drift are
added to the decision rates, a segment/recent-window-aware data-selection
strategy measurably beats all-history training.

Methodology (docs/drafts/COMPETITION_DIFFICULTY.md section 1 headroom method):
every strategy fits a linear model of the 13 observed features -> the decision
probability, differing only in *which training rows it selects*:

* ``all_history``  -- one pooled model over all history [0, 150).
* ``recent_window`` -- one pooled model over the last 30 days [120, 150).
* ``segment_aware`` -- one model per occupation over all history [0, 150).

The counterfactual truth is ``events_latent.p_decision_eventual`` / the
``candidates_latent`` projection.  Because the deterministic fee is known and
identical for every strategy, the ranking contribution of a strategy is exactly
its ``p_decision`` prediction; we therefore report the calibration error
(RMSE + signed bias) of each strategy's predicted ``p_decision`` on the
evaluation-window candidate set, plus the counterfactual fee metric for
completeness.  The "before" run disables the segment/drift effects and shows the
three strategies tie; the "after" run (the shipped defaults) shows the
recent-window and segment-aware selections measurably reduce the error.

Run from the synthetic_funnel package root:

    uv run python ../experiments/issue46_selection_effect.py
"""
import sqlite3
import tempfile
from collections import defaultdict
from contextlib import closing
from datetime import timedelta

import numpy as np
from sklearn.linear_model import LinearRegression

from synthetic_funnel import MarketConfig, write_market_dataset
from synthetic_funnel.market import OCCUPATION_INDEX
from synthetic_funnel.market_candidates import attach_latent_truth, candidate_rows
from synthetic_funnel.mechanism import SegmentDrift
from synthetic_funnel.shared import FEATURE_FIELDS, parse_datetime

MARKET_START = "2025-01-01T00:00:00+09:00"
TRAIN_END = 150
EVAL_START, EVAL_END = 150, 180
RECENT_WINDOW = 30
MAX_PER_USER_PER_DAY = 5

_FEATURE_SQL = {
    "seeker_skill": "u.user_skill", "required_skill": "i.required_skill",
    "desired_salary": "u.desired_salary", "offered_salary": "i.offered_salary",
    "wants_remote": "u.wants_remote", "allows_remote": "i.allows_remote",
    "skill_match": "l.skill_match", "skill_shortfall": "l.skill_shortfall",
    "overqualification": "l.overqualification", "salary_shortfall": "e.salary_shortfall",
    "remote_mismatch": "e.remote_mismatch", "other_applications": "e.other_applications",
    "competing_candidates": "e.competing_candidates",
}
_FEATURE_COLUMNS = ", ".join(f"{expr} AS {name}" for name, expr in _FEATURE_SQL.items())


def proof_config(seed, segment_drift):
    return MarketConfig(seed=seed, seeker_arrivals=50, job_arrivals=50, days=280,
                        warmup=90, application_days=20, posting_days=40, referrals=5,
                        segment_drift=segment_drift)


def _day_offset(value, start):
    return int((parse_datetime(value) - start).total_seconds() // 86400)


def selection_effect(seed, segment_drift):
    config = proof_config(seed, segment_drift)
    with tempfile.TemporaryDirectory() as temporary:
        manifest = write_market_dataset(config, temporary + "/ds", market_start=MARKET_START)
        start = parse_datetime(MARKET_START)
        segment_cfg = manifest["config"]["segment_drift"]
        days = manifest["config"]["days"]
        with closing(sqlite3.connect(temporary + "/ds/dataset.sqlite.db")) as connection:
            connection.row_factory = sqlite3.Row
            training = connection.execute(
                f"SELECT {_FEATURE_COLUMNS}, l.p_decision_eventual, u.occupation, e.shoukai_dt "
                "FROM events AS e JOIN events_latent AS l USING (event_id) "
                "JOIN user AS u USING (user_id) JOIN item AS i USING (item_id)"
            ).fetchall()

        history = [r for r in training if _day_offset(r["shoukai_dt"], start) < TRAIN_END]
        x_hist = np.stack([np.asarray([r[name] for name in FEATURE_FIELDS], dtype=float)
                           for r in history])
        y_hist = np.asarray([r["p_decision_eventual"] for r in history], dtype=float)
        hist_occ = np.asarray([OCCUPATION_INDEX[r["occupation"]] for r in history], dtype=int)
        hist_day = np.asarray([_day_offset(r["shoukai_dt"], start) for r in history], dtype=int)
        recent = hist_day >= TRAIN_END - RECENT_WINDOW

        pooled = LinearRegression().fit(x_hist, y_hist)
        recent_model = LinearRegression().fit(x_hist[recent], y_hist[recent])
        per_occ = {o: LinearRegression().fit(x_hist[hist_occ == o], y_hist[hist_occ == o])
                   for o in range(8) if int((hist_occ == o).sum()) >= 10}

        candidates = []
        with closing(sqlite3.connect(temporary + "/ds/dataset.sqlite.db")) as connection:
            for offset in range(EVAL_START, EVAL_END):
                day_dt = (start + timedelta(days=offset)).isoformat()
                for row in attach_latent_truth(candidate_rows(connection, day_dt, latent=True),
                                               seed=seed, segment_drift=segment_cfg,
                                               days=days, day=offset):
                    row["_day"] = offset
                    candidates.append(row)

        x_eval = np.stack([np.asarray([r[name] for name in FEATURE_FIELDS], dtype=float)
                           for r in candidates])
        y_eval = np.asarray([r["p_decision_eventual"] for r in candidates], dtype=float)
        occ_eval = np.asarray([OCCUPATION_INDEX[r["seeker_occupation"]] for r in candidates],
                              dtype=int)

        def prediction(strategy):
            if strategy == "all_history":
                return pooled.predict(x_eval)
            if strategy == "recent_window":
                return recent_model.predict(x_eval)
            values = np.full(len(candidates), np.nan)
            for o, model in per_occ.items():
                mask = occ_eval == o
                values[mask] = model.predict(x_eval[mask])
            return values

        calibration = {}
        for strategy in ("all_history", "recent_window", "segment_aware"):
            pred = prediction(strategy)
            residual = pred - y_eval
            calibration[strategy] = {
                "rmse": float(np.sqrt(np.mean(residual ** 2))),
                "bias": float(np.mean(residual)),
            }

        # Counterfactual fee metric (headroom method): top-5-per-user per day.
        fee_by_day = defaultdict(list)
        for row in candidates:
            fee_by_day[row["_day"]].append(row)
        fee_metric = {strategy: [] for strategy in
                      ("all_history", "recent_window", "segment_aware", "oracle")}
        for offset in sorted(fee_by_day):
            rows = fee_by_day[offset]
            x_day = np.stack([np.asarray([r[name] for name in FEATURE_FIELDS], dtype=float)
                              for r in rows])
            occ_day = np.asarray([OCCUPATION_INDEX[r["seeker_occupation"]] for r in rows],
                                 dtype=int)
            for strategy in ("all_history", "recent_window", "segment_aware"):
                if strategy == "all_history":
                    pred = pooled.predict(x_day)
                elif strategy == "recent_window":
                    pred = recent_model.predict(x_day)
                else:
                    pred = np.full(len(rows), np.nan)
                    for o, model in per_occ.items():
                        mask = occ_day == o
                        pred[mask] = model.predict(x_day[mask])
                score = pred * np.asarray([r["fee_if_decided_yen"] for r in rows], dtype=float)
                fee_metric[strategy].append(_select_metric(rows, score))
            oracle = np.asarray([r["p_decision_eventual"] for r in rows], dtype=float) * \
                np.asarray([r["fee_if_decided_yen"] for r in rows], dtype=float)
            fee_metric["oracle"].append(_select_metric(rows, oracle))
        fee = {strategy: float(np.mean(values)) for strategy, values in fee_metric.items()}
        return calibration, fee


def _select_metric(rows, score):
    order = np.argsort(-np.asarray(score, dtype=float))
    per_user = defaultdict(int)
    chosen = set()
    for index in order:
        if per_user[rows[index]["user_id"]] >= MAX_PER_USER_PER_DAY:
            continue
        per_user[rows[index]["user_id"]] += 1
        chosen.add(int(index))
    if not chosen:
        return float("nan")
    return float(np.mean([rows[i]["p_decision_eventual"] * rows[i]["fee_if_decided_yen"]
                          for i in chosen]))


def main():
    before = SegmentDrift(occupation_effects=(0.0,) * 8, region_effects=(0.0,) * 8,
                          drift_amplitude=0.0)
    after = SegmentDrift()
    print("mode,seed,strategy,rmse,bias,fee_metric")
    for label, drift in (("before", before), ("after", after)):
        for seed in (71, 72):
            calibration, fee = selection_effect(seed, drift)
            for strategy in ("all_history", "recent_window", "segment_aware"):
                c = calibration[strategy]
                print(f"{label},{seed},{strategy},{c['rmse']:.6f},{c['bias']:+.6f},"
                      f"{fee[strategy]:.4f}")
            print(f"{label},{seed},oracle,,,{fee['oracle']:.4f}")


if __name__ == "__main__":
    main()
