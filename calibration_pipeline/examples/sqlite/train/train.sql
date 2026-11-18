-- 合成の紹介履歴を、観測締切の異なる学習・校正・検証期間に分ける。
WITH periods(row_role, start_date, end_date, observed_through) AS (
    VALUES ('train',      '2024-01-01', '2024-02-01', '2024-05-01'),
           ('calibrate',  '2024-05-01', '2024-06-01', '2024-09-01'),
           ('validation', '2024-09-01', '2024-10-01', '2025-01-01')
),
referrals AS (
    SELECT p.row_role, p.observed_through, r.user_id, r.shoukai_dt, r.oubo_score, r.oubo_dt,
           datetime(r.shoukai_dt, '+3 months', 'floor') AS application_deadline
    FROM main.scored_referrals AS r
    JOIN periods AS p
      ON r.shoukai_dt >= p.start_date AND r.shoukai_dt < p.end_date
),
ranked_referrals AS (
    SELECT r.row_role, r.user_id, r.shoukai_dt, r.oubo_score,
           CASE WHEN r.oubo_dt BETWEEN r.shoukai_dt AND r.application_deadline
                THEN 1 ELSE 0 END AS label,
           ROW_NUMBER() OVER (PARTITION BY r.row_role ORDER BY random()) AS sample_rank
    FROM referrals AS r
    WHERE r.application_deadline <= datetime(r.observed_through)
)
SELECT r.row_role, r.oubo_score, f.seeker_skill, f.occupation, r.label
FROM ranked_referrals AS r
LEFT JOIN main.user_feature_daily AS f
  ON f.user_id = r.user_id AND f.snapshot_date = date(r.shoukai_dt)
WHERE r.sample_rank <= 100000
ORDER BY r.row_role, r.sample_rank;
