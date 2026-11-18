"""Daily batch-inference candidate recipes for the published market dataset.

The SQL strings here are the single source of truth for the recipe documented in
``docs/DAILY_CANDIDATES.md``; the same text is pasted into that page so the two
cannot silently disagree.  ``:day`` is an ISO 8601 datetime with the same UTC
offset as ``market_start`` (for example ``2025-01-16T00:00:00+09:00``).

``candidates`` holds only observable columns, including the item's deterministic
``fee_rate`` and ``fee_if_decided_yen`` (issue #34).  ``candidates_latent`` is for
organiser-side evaluation: stage one (SQL) adds the skill proxies and the fixed
motivations, stage two (:func:`attach_latent_truth`) calls the mechanism to add
the counterfactual probabilities, which SQL alone cannot compute.
"""

import numpy as np

from .mechanism import probability_arrays
from .rates import population_rates
from .shared import FEATURE_FIELDS, STAGES, ProbabilityConfig

# Observed candidate list.  Snapshot counts are now exact: events.outcome_dt
# records the observed terminal time of every cancellation (issue #32), so an
# applied case is "in flight" at :day only when neither kettei_dt nor outcome_dt
# has happened by then.  status/auto are intentionally not used as features.
DAILY_CANDIDATES_SQL = """\
SELECT
    :day AS day_dt,
    u.user_id AS user_id,
    i.item_id AS item_id,
    u.user_skill AS seeker_skill,
    i.required_skill AS required_skill,
    u.desired_salary AS desired_salary,
    i.offered_salary AS offered_salary,
    i.fee_rate AS fee_rate,
    i.offered_salary * i.fee_rate AS fee_if_decided_yen,
    u.wants_remote AS wants_remote,
    i.allows_remote AS allows_remote,
    MAX((u.desired_salary - i.offered_salary) / u.desired_salary, 0) AS salary_shortfall,
    u.wants_remote * (1 - i.allows_remote) AS remote_mismatch,
    (SELECT COUNT(*) FROM events AS o
      WHERE o.user_id = u.user_id
        AND o.oubo_dt IS NOT NULL AND o.oubo_dt <= :day
        AND (o.kettei_dt IS NULL OR o.kettei_dt > :day)
        AND (o.outcome_dt IS NULL OR o.outcome_dt > :day)) AS other_applications,
    (SELECT COUNT(*) FROM events AS c
      WHERE c.item_id = i.item_id
        AND c.oubo_dt IS NOT NULL AND c.oubo_dt <= :day
        AND (c.kettei_dt IS NULL OR c.kettei_dt > :day)
        AND (c.outcome_dt IS NULL OR c.outcome_dt > :day)) AS competing_candidates
FROM user AS u
JOIN item AS i ON i.occupation = u.occupation
WHERE u.arrival_dt <= :day AND (u.departure_dt IS NULL OR u.departure_dt > :day)
  AND i.arrival_dt <= :day AND (i.departure_dt IS NULL OR i.departure_dt > :day)
  AND (i.region = u.region OR i.allows_remote = 1)
  AND MAX(100, i.offered_salary - 100) <= u.desired_salary
  AND NOT EXISTS (SELECT 1 FROM events AS e
                  WHERE e.user_id = u.user_id AND e.item_id = i.item_id)
"""

# Stage one of candidates_latent: the same pairs plus the latent-derived feature
# proxies and the immutable motivations.  The p_* truth is added in Python.
DAILY_CANDIDATES_LATENT_SQL = """\
SELECT
    :day AS day_dt,
    u.user_id AS user_id,
    i.item_id AS item_id,
    u.user_skill AS seeker_skill,
    i.required_skill AS required_skill,
    u.desired_salary AS desired_salary,
    i.offered_salary AS offered_salary,
    i.fee_rate AS fee_rate,
    i.offered_salary * i.fee_rate AS fee_if_decided_yen,
    u.wants_remote AS wants_remote,
    i.allows_remote AS allows_remote,
    MAX((u.desired_salary - i.offered_salary) / u.desired_salary, 0) AS salary_shortfall,
    u.wants_remote * (1 - i.allows_remote) AS remote_mismatch,
    (SELECT COUNT(*) FROM events AS o
      WHERE o.user_id = u.user_id
        AND o.oubo_dt IS NOT NULL AND o.oubo_dt <= :day
        AND (o.kettei_dt IS NULL OR o.kettei_dt > :day)
        AND (o.outcome_dt IS NULL OR o.outcome_dt > :day)) AS other_applications,
    (SELECT COUNT(*) FROM events AS c
      WHERE c.item_id = i.item_id
        AND c.oubo_dt IS NOT NULL AND c.oubo_dt <= :day
        AND (c.kettei_dt IS NULL OR c.kettei_dt > :day)
        AND (c.outcome_dt IS NULL OR c.outcome_dt > :day)) AS competing_candidates,
    1 - ABS(u.user_skill - i.required_skill) AS skill_match,
    MAX(i.required_skill - u.user_skill, 0) AS skill_shortfall,
    MAX(u.user_skill - i.required_skill, 0) AS overqualification,
    ul.user_motivation AS user_motivation,
    il.item_motivation AS item_motivation
FROM user AS u
JOIN item AS i ON i.occupation = u.occupation
JOIN user_latent AS ul USING (user_id)
JOIN item_latent AS il USING (item_id)
WHERE u.arrival_dt <= :day AND (u.departure_dt IS NULL OR u.departure_dt > :day)
  AND i.arrival_dt <= :day AND (i.departure_dt IS NULL OR i.departure_dt > :day)
  AND (i.region = u.region OR i.allows_remote = 1)
  AND MAX(100, i.offered_salary - 100) <= u.desired_salary
  AND NOT EXISTS (SELECT 1 FROM events AS e
                  WHERE e.user_id = u.user_id AND e.item_id = i.item_id)
"""

# Alternative for tools that cannot bind parameters: write the batch day into a
# one-row table and select a view whose day filter reads from that table.
BATCH_DAY_TABLE_SQL = "CREATE TABLE IF NOT EXISTS batch_day (day_dt TEXT)"
DAILY_CANDIDATES_VIEW_SQL = (
    "CREATE VIEW IF NOT EXISTS daily_candidates AS\n"
    + DAILY_CANDIDATES_SQL.replace(":day", "(SELECT day_dt FROM batch_day)")
)


def candidate_rows(connection, day, *, latent=False):
    """Run the daily recipe with ``:day`` bound and return a list of dicts."""
    sql = DAILY_CANDIDATES_LATENT_SQL if latent else DAILY_CANDIDATES_SQL
    cursor = connection.execute(sql, {"day": day})
    columns = [description[0] for description in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def attach_latent_truth(rows, *, seed=71):
    """Add counterfactual p_* columns to stage-one candidate rows.

    ``seed`` must be the market seed recorded in ``manifest.json`` so the anchor
    intercepts match the generating market.  The snapshot counts are exact now
    that cancellations are observed, but p_* is still an estimate for a pair
    that has never been introduced.
    """
    base = ProbabilityConfig(seed=seed)
    anchors = population_rates(base).intercepts
    observed = {
        field: np.asarray([row[field] for row in rows], dtype=float) for field in FEATURE_FIELDS
    }
    seeker_motivation = np.asarray([row["user_motivation"] for row in rows], dtype=float)
    employer_motivation = np.asarray([row["item_motivation"] for row in rows], dtype=float)
    gates = probability_arrays(base, observed, seeker_motivation, employer_motivation, anchors)
    enriched = []
    for index, row in enumerate(rows):
        result = dict(row)
        for stage, name in enumerate(STAGES):
            candidate = float(gates[index, stage, 0])
            employer = float(gates[index, stage, 1])
            result[f"p_candidate_{name}"] = candidate
            result[f"p_employer_{name}"] = employer
            result[f"p_{name}_eventual"] = candidate * employer
        enriched.append(result)
    return enriched
