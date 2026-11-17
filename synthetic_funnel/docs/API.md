# 設定とPython API

## 設定リファレンス

CLIのJSONとPythonの `Config.from_dict(mapping)` は同じ項目を受け取ります。省略した項目には既定値が入り、未知のキーはエラーになります。`Config(...)` で直接作る場合は `Delay` などのデータクラスを渡します。三要素の設定はすべて応募、書類通過、決定の順です。

| 設定 | 用途と既定値 |
| --- | --- |
| `rows`, `seed` | 出力する紹介件数は1,000,000件。生成用の乱数seedは42。 |
| `referral_start`, `referral_end`, `as_of` | 紹介日時の範囲と観測締切。UTCオフセット付きISO 8601で指定し、開始 ≤ 終了 ≤ 締切にする。 |
| `application_delay`, `screening_delay`, `decision_delay` | 各遷移の所要日数。既定の `(mean_days, std_days)` は順に `(3, 2)`、`(5, 4)`、`(30, 20)`。 |
| `base_probabilities`, `probability_half_widths` | 前段到達者の平均目標 `(0.01, 0.10, 0.07)`、ペアごとの上下幅 `(0.005, 0.05, 0.035)`。下限と上限はともに0と1の間。 |
| `reference_rows`, `reference_seed` | 平均目標を合わせるための参照集団。既定は50,000件と90210。件数は100以上。 |
| `skill_effects`, `mechanism` | スキル適合、給与、リモート希望とのずれ、隠れた相性、競合、決定時のギャップを設定する。[確率モデル](PROBABILITY_MODEL.md)・[Mechanismの全項目](../src/synthetic_funnel/mechanism.py) |
| `population` | 固定求人件数 `job_count`（既定1,000）と、スキル、給与、リモート希望・可否、競合件数の分布を設定する。[Populationの全項目](../src/synthetic_funnel/population.py) |
| `legacy_scores` | 元モデルスコアの学習有無と学習履歴の規模を設定する。 |

日数の設定は切断前の正規分布の母数です。成功した遷移の所要日数だけ独立に抽選し、負値を引いた場合は引き直します。`std_days: 0` は固定日数です。小数日は日時に反映されます。

### 元モデルスコア

`legacy_scores.enabled` の既定値は `true` です。標準化とロジスティック回帰を使い、各段階で用意した正例と負例を1対1にして学習します。学習履歴は生成する紹介の前に結果が成熟するように用意し、ラベルは紹介から共通3暦月以内の到達で定義します。出力スコアは、この抽出分布における正例の推定確率で、`predict_proba` の正例側に相当します。

| 設定 | 既定値と変更方法 |
| --- | --- |
| `training_rows` | 共通の学習履歴は1,000,000件。 |
| `training_window_days` | 既定は `null` で共通履歴を使う。`[1, 30, 365]` なら、紹介日、応募日、通過日をそれぞれ起点とする期間を指定できる。 |
| `training_referrals_per_day` | 段階別の学習期間を使うときの1日あたりの生成件数。既定は10,000。 |
| `negative_reservoir_size` | 段階別の学習期間を使うときに保持する負例候補の上限。既定は100,000。全正例を保持するため、正例数以上の値が必要。 |
| `seed` | 元モデル学習用のseed。既定は1931。 |

学習には各段階で正例が2件以上あり、負例が正例数以上必要です。不足する場合は `training_rows` を増やします。段階別の学習期間を使う場合は、該当段階の期間または日あたり件数を増やします。少量のデータでファイルや結合を確認するときは `"legacy_scores": {"enabled": false}` を使えます。

## 公開Python API

| API | 入力と返り値 |
| --- | --- |
| `Config`, `Delay`, `Population`, `Mechanism`, `LegacyScores` | 生成や学習の条件を設定するデータクラス。 |
| `generate(config)` | 1紹介ずつ `(eventsの辞書, truthの辞書)` を返すイテレーター。未観測の日時は空文字。`legacy_scores.enabled=False` の場合、辞書にスコアキーは入らない。 |
| `write_dataset(config, output, progress=None, *, output_format="sqlite")` | 新しいディレクトリにデータとmanifestを保存し、manifestの辞書を返す。進捗コールバックは10万件ごとと最終件で総件数を受け取る。 |
| `pair_features(seeker_skill, required_skill, desired_salary, offered_salary, wants_remote, allows_remote)` | スキル0〜1、正の給与、リモート希望・可否0または1を受け取り、ペアの特徴辞書を返す。 |
| `transition_probabilities(config, features, seeker_affinity=0.0, employer_affinity=0.0)` | 段階ごとの `(求職者側確率, 企業側確率)` を返す。両者の積がその段階の条件付き確率。各行の真値を再現するには、シミュレーションで使った相性と競合件数も渡す。 |
| `population_rates(config)` | `expected_rates`（参照集団の条件付き平均）、`intercepts`、`reference_rows`、`reference_seed` を持つ結果を返す。 |

以下を `synthetic_funnel/` で実行します。

```powershell
@'
from itertools import islice

from synthetic_funnel import Config, LegacyScores, generate

config = Config(rows=12, seed=42, reference_rows=1000,
                legacy_scores=LegacyScores(enabled=False))
for event, truth in islice(generate(config), 2):
    p = truth["p_application_eventual"]
    print(event["event_id"], f"{p:.4%}")
'@ | uv run --locked python -
```

別の `uv` プロジェクトから使うには、そのプロジェクトから見たリポジトリ内の `synthetic_funnel/` を指定して `uv add --editable ../synthetic_funnel` を実行します。パスは両プロジェクトが同じ階層にある場合の例です。配布用のwheelはパッケージのルートで `uv build --wheel` を実行して作れます。
