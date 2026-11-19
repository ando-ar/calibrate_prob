-- Adapt the market's prefixed IDs and offset-bearing timestamps to the example's local calendar.
-- Attributes are immutable in the user/item masters, so they are joined here instead of events.
-- user_latent/item_latent hold one hidden motivation row per master participant; the demo views
-- do not need them, so no JOIN is added (JOIN on user_id/item_id if a latent column is needed).
-- During the market's warmup the model_*_score columns are NULL; those referrals cannot be
-- trained or calibrated, so the view excludes them explicitly. train.sql repeats the guard.
CREATE VIEW scored_referrals AS
SELECT 10000000 + CAST(substr(e.user_id, length('user-') + 1) AS INTEGER) AS user_id,
       20000000 + CAST(substr(e.item_id, length('item-') + 1) AS INTEGER) AS item_id,
       replace(substr(e.shoukai_dt, 1, 19), 'T', ' ') AS shoukai_dt,
       e.model_apply_score,
       replace(substr(e.oubo_dt, 1, 19), 'T', ' ') AS oubo_dt,
       u.user_skill AS seeker_skill,
       u.occupation AS occupation
FROM events AS e
JOIN user AS u ON u.user_id = e.user_id
WHERE e.model_apply_score IS NOT NULL;

CREATE TABLE user_feature_daily (
    user_id INTEGER NOT NULL,
    snapshot_date TEXT NOT NULL,
    seeker_skill REAL NOT NULL,
    occupation TEXT,
    PRIMARY KEY (user_id, snapshot_date)
);

-- A user can receive several referrals on one day; their master attributes do not change.
-- Some users have no occupation to exercise NULL handling.
INSERT INTO user_feature_daily
SELECT DISTINCT user_id, date(shoukai_dt), seeker_skill,
       CASE WHEN (user_id - 10000000) % 7 = 0 THEN NULL ELSE CAST(occupation AS TEXT) END
FROM scored_referrals;
