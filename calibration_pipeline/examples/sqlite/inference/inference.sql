-- 推論日の特徴量を結合し、候補行ごとにモデルの入力を返す。
SELECT c.candidate_row_id, c.model_apply_score, f.seeker_skill, f.occupation
FROM calibration_candidates AS c
LEFT JOIN main.user_feature_daily AS f
  ON f.user_id = c.user_id AND f.snapshot_date = date(c.shoukai_dt)
ORDER BY c.candidate_row_id DESC;
