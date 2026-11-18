-- Adapt the generator's offset-bearing timestamps to the example's local calendar.
CREATE VIEW scored_referrals AS
SELECT 10000000 + CAST(substr(user_id, length('user-') + 1) AS INTEGER) AS user_id,
       20000000 + CAST(substr(item_id, length('item-') + 1) AS INTEGER) AS item_id,
       replace(substr(shoukai_dt, 1, 19), 'T', ' ') AS shoukai_dt,
       legacy_apply_score AS oubo_score, replace(substr(oubo_dt, 1, 19), 'T', ' ') AS oubo_dt, seeker_skill
FROM events;

CREATE TABLE user_feature_daily (
    user_id INTEGER NOT NULL,
    snapshot_date TEXT NOT NULL,
    seeker_skill REAL NOT NULL,
    occupation TEXT,
    PRIMARY KEY (user_id, snapshot_date)
);

-- Synthetic user attribute. Some users have no occupation to exercise NULL handling.
INSERT INTO user_feature_daily
SELECT user_id, date(shoukai_dt), seeker_skill,
       CASE WHEN user_id % 7 = 0 THEN NULL
            WHEN user_id % 3 = 0 THEN 'engineering'
            WHEN user_id % 3 = 1 THEN 'sales'
            ELSE 'office' END
FROM scored_referrals;
