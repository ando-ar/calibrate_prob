# 設定とPython API

生成モードは継続市場のみです。固定件数の行ベース生成（`Config`、`generate`、`write_dataset`、`legacy_scores`、`legacy_models.json`）は廃止されました。市場のルールは[継続市場のシミュレーション](MARKET.md)、出力列は[出力データ仕様](DATA_FORMAT.md)を参照してください。

## CLIのJSON設定

CLIのJSONは `MarketConfig` の項目に加えて `market_start` を受け取ります。省略した項目には既定値が入り、未知のキーはエラーになります。廃止された `rows`、`legacy_scores`、`legacy_models`、`retain`、`referral_start`、`referral_end`、`as_of` を指定するとエラーになります。

| 設定 | 用途と既定値 |
| --- | --- |
| `market_start` | 市場開始日時。UTCオフセット付きISO 8601で指定する（必須）。 |
| `seed` | 乱数seed。71。 |
| `seeker_arrivals`, `job_arrivals` | 1日あたりの求職者・求人の流入数。それぞれ50。 |
| `days` | 生成日数。1,825日。 |
| `warmup` | 市場内ウォームアップ学習に使う日数。365日。 |
| `application_days`, `posting_days` | 求職者の応募可能日数20日、求人の掲載日数40日。 |
| `referrals` | 1人1日あたりの紹介上限。0〜5件、既定5件。 |
| `training_window_days`, `training_referrals_per_day`, `negative_reservoir_size` | warmup 学習の期間・密度・負例プール。`[1, 30, 365]`、10,000、100,000。 |

流入数はCLIの `--seeker-arrivals` / `--job-arrivals` でも上書きできます。`--run-dir` と `--output --config` のどちらでも使えます。

## 公開Python API

| API | 入力と返り値 |
| --- | --- |
| `MarketConfig` | 市場の設定データクラス。`seed`、`seeker_arrivals`、`job_arrivals`、`days`、`warmup`、`application_days`、`posting_days`、`referrals`、warmup学習の設定。 |
| `write_market_dataset(config, output, *, market_start, output_format="sqlite", progress=None)` | 市場をシミュレーションし、warmup学習で全紹介をスコア付けして、新しいディレクトリに5テーブルと `manifest.json` を保存する。manifestの辞書を返す。 |
| `simulate(config, *, progress=None, snapshot_days=None, resume=None, focal_index=None, branch_seed=None, daily_stats=None)` | `synthetic_funnel.market` の関数。紹介行と求職者・求人の属性配列を返す。出力せずに検証するときに使う。 |
| `extract(rows, horizon, stage, period, as_of, mode, ...)` | `synthetic_funnel.market` の関数。成熟した段階のリスク集合を抽出する。 |
| `train_warmup_models(rows, seekers, jobs, config, start)` / `score_warmup_referrals(...)` | `synthetic_funnel.market_training` の関数。warmup紹介だけを使う学習と推論。 |
| `pair_features(seeker_skill, required_skill, desired_salary, offered_salary, wants_remote, allows_remote)` | スキル0〜1、正の給与、リモート希望・可否0または1を受け取り、ペアの特徴辞書を返す。 |
| `transition_probabilities(config, features, seeker_affinity=0.0, employer_affinity=0.0)` | 段階ごとの `(求職者側確率, 企業側確率)` を返す。両者の積がその段階の条件付き確率。 |
| `population_rates(config)` | `expected_rates`（参照集団の条件付き平均）、`intercepts`、`reference_rows`、`reference_seed` を持つ `RateAnchor` を返す。 |

確率設定を直接試すときは、`synthetic_funnel.shared` の `ProbabilityConfig` を `transition_probabilities` や `population_rates` に渡せます。既定値は `base_probabilities=(0.01, 0.10, 0.07)`、`probability_half_widths=(0.005, 0.05, 0.035)`、`skill_effects=(2.0, 1.5, 1.0)` です。市場のスコア学習と確率計算はこの共通設定を共有し、市場シミュレーションの乱数系列には影響しません。

以下を `synthetic_funnel/` で実行します。

```powershell
@'
from synthetic_funnel import MarketConfig, write_market_dataset

manifest = write_market_dataset(
    MarketConfig(days=120, warmup=60, seeker_arrivals=8, job_arrivals=8),
    "runs/python_market/output",
    market_start="2025-01-01T00:00:00+09:00",
)
print(manifest["row_counts"], manifest["training"]["status"])
'@ | uv run --locked python -
```

別の `uv` プロジェクトから使うには、そのプロジェクトから見たリポジトリ内の `synthetic_funnel/` を指定して `uv add --editable ../synthetic_funnel` を実行します。パスは両プロジェクトが同じ階層にある場合の例です。配布用のwheelはパッケージのルートで `uv build --wheel` を実行して作れます。
