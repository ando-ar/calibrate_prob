# 継続市場のシミュレーション

同じ求職者への複数紹介や、市場参加者の退出を含めた検証には継続市場を使います。ファイル出力は通常の[紹介履歴](DATA_FORMAT.md)と同じ SQLite・Parquet・CSV から選べます。抽出やリプレイの検証では、`synthetic_funnel.market` の `simulate()` が返す NumPy 配列を使えます。

## 市場のルール

- スキルや希望給与などの属性を参加時に決め、在籍中は固定します。求職者と求人は設定した件数ずつ毎日参加します。
- 既定の応募可能期間は20日、求人掲載期間は40日、紹介は求職者1人につき1日最大5件です。職種が一致し、希望給与が求人レンジの下限を上回り、勤務地が一致するか全国リモート可のペアを抽出します。同じ求職者と求人の組み合わせは再紹介しません。
- 決定すると双方が退出し、関連する選考が終了します。求職者は応募期間後に進行中の選考が終わると退出します。求人は掲載期限でも退出します。
- 行に保存される `pa`、`ps`、`pd` は紹介時点の基礎移行確率です。紹介時点の他社応募件数と他候補者数を含めて計算します。その後に起きる退出や掲載期限の影響は、シミュレーションした到達結果に反映されます。

`MarketConfig` では `seed`、1日あたりの流入数 `seeker_arrivals` と `job_arrivals`、生成日数 `days`、応募可能日数 `application_days`、掲載日数 `posting_days`、紹介上限 `referrals` を設定します。既定の流入はそれぞれ50件/日、生成日数は1,825日です。

## CLIからデータセットを保存する

`synthetic_funnel/` で実行します。継続市場のスコアには、通常の紹介履歴の出力にある `legacy_models.json` を使います。`legacy_scores.enabled` を `true` にすると、応募・通過・決定の3モデルがこのJSONに保存されます。学習締切 `training_cutoff` が市場開始日時 `market_start` 以前のファイルを指定してください。

既存のモデルがない場合は、次の例で動作確認用のモデルを作れます。少量の学習データでも決定ラベルを得られるよう、この例の紹介履歴では基準率を高めています。

```powershell
New-Item -ItemType Directory -Force runs/market_model/input, runs/market_demo/input | Out-Null
@'
{
  "rows": 20,
  "reference_rows": 1000,
  "base_probabilities": [0.35, 0.45, 0.35],
  "probability_half_widths": [0.10, 0.10, 0.10],
  "legacy_scores": {"enabled": true, "training_rows": 3000}
}
'@ | Set-Content -Encoding ascii runs/market_model/input/config.json
uv run --locked synthetic-funnel --run-dir runs/market_model --format sqlite
```

市場の設定を `runs/market_demo/input/config.json` に置き、`--market` を指定します。モデルへの相対パスは、この設定ファイルがあるディレクトリを基準にします。

```powershell
@'
{
  "market_start": "2025-01-01T00:00:00+09:00",
  "legacy_models": "../../market_model/output/legacy_models.json",
  "days": 90,
  "seeker_arrivals": 8,
  "job_arrivals": 8
}
'@ | Set-Content -Encoding ascii runs/market_demo/input/config.json
uv run --locked synthetic-funnel --market --run-dir runs/market_demo --format sqlite
```

SQLite では `runs/market_demo/output/dataset.sqlite.db` に下の5テーブルが入ります。`--format parquet` と `--format csv` ではテーブルごとにファイルを保存します。再実行するときは別の実行ディレクトリを使います。`manifest.json` でデータ件数、観測終了日時、モデルのハッシュと学習締切を確認できます。

| テーブル | 1行の単位とキー | 主な内容 |
| --- | --- | --- |
| `events` | 紹介ごと。`event_id` | 求職者と求人のID、マッチ特徴量、3つの `model_*_score`、紹介および観測された到達日時。参加者の属性は不変のため `user` / `item` マスターに保存します。 |
| `events_latent` | 紹介ごと。`event_id` で `events` と1対1 | 市場からの退出や掲載終了を適用する前の各段階の条件付き確率 `p_*_eventual`、相性、応募・通過確率の差。片側確率、潜在到達日時、失敗理由などの記録のない真値は欠損。 |
| `user` | 求職者ごと。`user_id` | 参加日時、スキル、希望給与、職種、地域、リモート希望、隠れた相性、退出日時 `departure_dt`（在籍中は欠損）。 |
| `item` | 求人ごと。`item_id` | 参加日時、要求スキル、提示給与、職種、地域、リモート可否、隠れた相性、退出日時 `departure_dt`（在籍中は欠損）。 |
| `daily` | 市場の日ごと。`day_dt` | 日末の在籍求職者数 `live_users` と求人数 `live_items`、当日の流入数 `new_users` / `new_items`、紹介数 `referrals`、当日の到達数 `applications` / `screenings` / `decisions`。 |

日時は `market_start` に経過日数を足し、UTCオフセット付きのISO 8601で保存します。観測終了までに到達が記録されなかった段階は `NULL`（CSVでは空欄）です。`p_*_eventual` は紹介時点で計算される確率で、その後の市場の打ち切りによって、実績の到達率は下がり得ます。`model_*_score` は指定したモデルを紹介時点の特徴量で推論した値です。市場の確率や観測結果はスコア入力に使いません。`departure_dt` は決定、応募期間後の自然退出（求人では掲載期限）で市場から退出した日時で、観測終了時点で在籍している参加者は `NULL` です。

Pythonで保存する場合も同じデータセットを生成できます。

```python
from synthetic_funnel import MarketConfig, write_market_dataset

write_market_dataset(
    MarketConfig(days=90, seeker_arrivals=8, job_arrivals=8),
    "runs/market_python/output",
    market_start="2025-01-01T00:00:00+09:00",
    legacy_models="runs/market_model/output/legacy_models.json",
    output_format="sqlite",
)
```

## 紹介を一様に抽出する

以下を `synthetic_funnel/` で実行します。紹介後30日以内の応募が評価できる最初の30日間の紹介から、5件を重複なく抽出します。

```powershell
@'
from synthetic_funnel.market import MarketConfig, extract, simulate

rows, seekers, jobs = simulate(MarketConfig(seeker_arrivals=8, job_arrivals=8, days=90))
horizon = rows["day"] + 30.0
sample = extract(rows, horizon, stage=0, period=(0, 30), as_of=90,
                 mode="uniform", seed=42, sample_size=5)
print(sample["population"], rows[sample["indices"]][["seeker", "job", "day"]])
'@ | uv run --locked python -
```

`simulate` は構造化配列 `rows` と、求職者・求人の属性配列を返します。`rows["seeker"]` と `rows["job"]` は属性配列の行インデックスです。`rows["day"]` は紹介日、`application`、`screening`、`decision` は各到達日で、未観測なら `NaN` です。

`retain="applications"` を指定すると、観測された応募がある紹介だけを残せます。応募率の評価に必要な分母を除外するため、通過・決定の検証だけに使います。この設定は `simulate()`、`write_market_dataset()`、市場CLIの設定で指定できます。プロフィールと `daily` の紹介数は市場全体の情報です。

## 学習用データと評価用データを取り分ける

`extract` の `horizon` は行ごとの判定期限を並べた配列です。時刻はすべて市場の開始日を0とする日数です。`as_of` までに `horizon` に達した行が抽出対象です。未観測の結果を負例に含めないよう、`as_of` はシミュレーションの終了境界 `MarketConfig.days` 以下に設定します。`period=(start, end)` は開始を含み、終了を含みません。

| `stage` | 抽出対象となる分母 | `period` で絞る日時 |
| --- | --- | --- |
| `0` | 紹介 | 紹介日 |
| `1` | 期限内に応募 | 応募日 |
| `2` | 期限内に書類通過 | 通過日 |

- 校正と評価では `mode="uniform"` で `sample_size` を指定します。抽出された `indices` と対象件数 `population` が返ります。正負を分けずに抽出するため、少数クラスの件数は標本ごとに変わります。
- 学習では `mode="matched"` で `positive_count`（正の整数または `"all"`）と `negative_ratio`（正例1件につける負例数）を指定できます。応募の負例は同じ求職者・同じ紹介日から、通過と決定の負例は該当する分母全体から選びます。正例1件の中では負例を重複させず、別の正例間で再利用できます。`matches` には正例と負例の行インデックスの組が入ります。
- 指定数を取れない場合は `SamplingError` が発生します。`positive_eligibility="has_matched_negatives"` を指定すると、負例が必要数ある正例に限定でき、除外した正例数は `excluded_positives` に入ります。
