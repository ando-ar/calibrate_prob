"""Small synthetic fixture to exercise the whole pipeline, not a market simulator."""
from pathlib import Path

import numpy as np
import pandas as pd


def generate(path: Path, rows: int = 18000, seed: int = 42):
    rng = np.random.default_rng(seed)
    # Concentrate rows on representative days so daily budgets bind in the demo.
    months = rng.integers(0, 42, rows)
    anchor = pd.DatetimeIndex([pd.Timestamp("2023-01-15") + pd.DateOffset(months=int(m)) for m in months])
    skill = rng.uniform(0, 1, rows)
    gap = rng.normal(0, 1, rows)
    probability = 1 / (1 + np.exp(-(-3.4 + 2 * skill - 0.4 * abs(gap))))
    applied = rng.random(rows) < (0.25 + 0.3 * skill)
    screened = applied & (rng.random(rows) < 0.4)
    decided = screened & (rng.random(rows) < np.minimum(1, probability / 0.15))
    lower = rng.uniform(400, 900, rows)
    upper = lower + rng.uniform(50, 300, rows)
    application = pd.Series(anchor + pd.Timedelta(days=2)).where(applied)
    screen = pd.Series(anchor + pd.Timedelta(days=10)).where(screened)
    decision = pd.Series(anchor + pd.to_timedelta(rng.integers(20, 120, rows), unit="D")).where(decided)
    frame = pd.DataFrame({"event_id": [f"e{i}" for i in range(rows)],
        "user_id": rng.integers(0, 20, rows).astype(str), "item_id": rng.integers(0, 800, rows).astype(str),
        "shoukai_dt": anchor, "oubo_dt": application, "shorui_tsuuka_dt": screen, "kettei_dt": decision,
        "skill_match": skill, "salary_gap": gap, "old_application_score": skill + rng.normal(0, 0.1, rows),
        "old_screen_score": skill * 0.6 + rng.normal(0, 0.2, rows),
        "market": rng.choice(["standard", "narrow"], rows), "role": rng.choice(["sales", "engineering"], rows),
        "salary_lower_man": lower, "salary_upper_man": upper, "desired_salary_man": lower + rng.normal(100, 150, rows),
        "fee_yen": np.where(decided, (lower + upper) / 2 * 10000 / 3 * rng.lognormal(0, 0.1, rows), np.nan)})
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite demo data: {path}")
    frame.to_parquet(path, index=False)
    return {"path": str(path), "rows": rows, "synthetic": True}
