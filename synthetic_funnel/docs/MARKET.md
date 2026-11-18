# 継続市場のシミュレーション

求職者と求人が毎日入れ替わる市場をシミュレーションし、紹介から応募、書類通過、入社決定までの履歴を生成します。単発の固定件数生成モードは廃止され、この市場生成が唯一の生成モードです。出力テーブルの列と意味は[出力データ仕様](DATA_FORMAT.md)を参照してください。

## 市場のルール

- スキルや希望給与などの属性は参加時に決め、在籍中は固定します。求職者と求人は設定した件数ずつ毎日参加します。
- 求職者の応募可能期間は `application_days`（既定20日）、求人の掲載期間は `posting_days`（既定40日）です。
- 紹介は求職者1人につき1日最大 `referrals` 件（既定5件、上限5）です。職種が一致し、希望給与が求人レンジの下限を上回り、勤務地が一致するか全国リモート可のペアを抽出します。同じ求職者と求人の組み合わせは再紹介しません。
- 決定すると双方が退出し、関連する選考が終了します。求職者は応募可能期間後に進行中の選考が終わると退出します。求人は掲載期限でも退出します。退出理由は保存せず、退出日時だけを `user.departure_dt` と `item.departure_dt` に記録します。
- 観測された到達（`events` の日時列）は、市場の退出や掲載期限、観測締切の影響を受けます。これらの影響を無視した反実仮の真値は `events_latent` に保存します。紹介時点の `p_*_eventual` は紹介時点の他社応募件数と他候補者数を含めて計算します。

`MarketConfig` では次の項目を設定します。

| 設定 | 用途と既定値 |
| --- | --- |
| `seed` | 乱数seed。71。 |
| `seeker_arrivals`, `job_arrivals` | 1日あたりの求職者・求人の流入数。それぞれ50件。 |
| `days` | 生成日数。1,825日。 |
| `warmup` | 市場内ウォームアップ学習に使う期間の日数。365日。 |
| `application_days` | 求職者の応募可能日数。20日。 |
| `posting_days` | 求人の掲載日数。40日。 |
| `referrals` | 1日あたりの1人当たり紹介上限。0〜5件。既定5件。 |

## 市場内ウォームアップ学習

スコアは外部の学習済みモデルファイルではなく、市場自身の紹介履歴から学習します。

1. 市場開始から `warmup` 日までの紹介履歴を蓄積します。
2. warmup の紹介で、応募・書類通過・決定の3段階のモデルを学習します。
3. 学習済みモデルを以降の紹介に適用し、`model_apply_score`、`model_screen_score`、`model_decision_score` にスコアを記録します。

- warmup 期間中の紹介のスコア列はNULLです。warmup の `events` も出力に含まれるため、学習データを確認できます。
- 旧仕様の `legacy_models.json` を作って渡す2段構成は廃止されました。manifest の `training_cutoff` と `warmup_end` で学習データと本運用データを区別できます。
- `warmup` が `days` 以上のときは、すべての紹介のスコア列がNULLになります。

## CLIからデータセットを保存する

`synthetic_funnel/` で実行します。`--run-dir` は `input/config.json` を読み、同じ場所に `output/` を作ります。

```powershell
$run = "runs/market-$(Get-Date -Format 'yyyyMMdd-HHmmssfff')"
New-Item -ItemType Directory -Path "$run/input" | Out-Null

@'
{
  "market_start": "2025-01-01T00:00:00+09:00",
  "days": 90,
  "seed": 71,
  "seeker_arrivals": 8,
  "job_arrivals": 8
}
'@ | Set-Content -Encoding ascii "$run/input/config.json"
uv run --locked synthetic-funnel --run-dir $run --format sqlite
```

`--seeker-arrivals` と `--job-arrivals` を指定すると、設定ファイルの1日あたり流入数を上書きできます。

```powershell
uv run --locked synthetic-funnel --run-dir $run --seeker-arrivals 12 --job-arrivals 12
```

`--format parquet` と `--format csv` で形式を指定できます。出力先が存在すると停止します。同じ条件で再実行するには、新しい実行ディレクトリに `input/config.json` を置きます。`manifest.json` でテーブルごとの件数、観測終了日時、`training_cutoff` と `warmup_end`、生成時間を確認できます。

SQLite では `runs/<実行ディレクトリ>/output/dataset.sqlite.db` に次の5テーブルが入ります。

| テーブル | 1行の単位とキー | 主な内容 |
| --- | --- | --- |
| `user` | 求職者の1参加。`user_id` | 参加日時、スキル、希望給与、職種、地域、リモート希望、隠れた相性、退出日時。 |
| `item` | 求人の1掲載。`item_id` | 参加日時、要求スキル、提示給与、職種、地域、リモート可否、隠れた相性、退出日時。 |
| `events` | 紹介1件。`event_id` | ユーザと求人のID、紹介時点のペア特徴量、3つの `model_*_score`、紹介および観測された到達日時。 |
| `events_latent` | 紹介1件。`event_id` で `events` と1対1 | 退出や打切りを無視した各段階の条件付き確率 `p_*_eventual`、片側確率 `p_candidate_*` / `p_employer_*`、相性、応募・通過確率の差、潜在到達日時、失敗段階と理由。 |
| `daily` | 市場の1日。`day_dt` | 日末の在籍数、当日の流入・紹介・到達件数。 |

日時は `market_start` に経過時間を足し、UTCオフセット付きのISO 8601で保存します。観測終了までに到達が記録されなかった段階はNULL（CSVでは空欄）です。`model_*_score` は warmup で学習したモデルを紹介時点の特徴量で推論した値です。市場の確率や観測結果はスコア入力に使いません。`events_latent` の `p_candidate_*` / `p_employer_*` の積は `p_*_eventual` に等しく、潜在到達日時と失敗段階・理由は退出や掲載期限、選考取消を無視した反実仮の値です。

## Pythonから保存する

```python
from synthetic_funnel import MarketConfig, write_market_dataset

manifest = write_market_dataset(
    MarketConfig(days=90, seeker_arrivals=8, job_arrivals=8),
    "runs/market_python/output",
    market_start="2025-01-01T00:00:00+09:00",
    output_format="sqlite",
)
```

`write_market_dataset` は新しいディレクトリにデータとmanifestを保存し、manifestの辞書を返します。モデルは市場内で学習されるため、旧仕様の `legacy_models` 引数は廃止されました。

## 後段校正のために紹介を絞り込む

すべての紹介が出力されます（旧仕様の `retain="applications"` は廃止）。応募率の校正は全紹介を分母に使えます。書類通過・決定の校正では、分母を揃えるために応募済み行などへ絞り込みます。

### SQLで絞り込む

応募済みの行だけを抽出します。書類通過の校正の分母です。

```sql
SELECT e.*, l.*
FROM events AS e
JOIN events_latent AS l USING (event_id)
WHERE e.oubo_dt IS NOT NULL
```

決定の校正の分母は、書類通過済みの行です。

```sql
SELECT e.*, l.*
FROM events AS e
JOIN events_latent AS l USING (event_id)
WHERE e.shorui_tsuuka_dt IS NOT NULL
```

ラベルの成熟判定（紹介から3暦月以内を評価するときの締切との比較）は[出力データ仕様](DATA_FORMAT.md)を参照してください。

### simulate() と extract() で抽出する

保存せずに検証や抽出をするときは、`synthetic_funnel.market` の `simulate()` と `extract()` を使えます。以下を `synthetic_funnel/` で実行します。紹介後30日以内の応募が評価できる最初の30日間の紹介から、5件を重複なく抽出します。

```python
from synthetic_funnel.market import MarketConfig, extract, simulate

rows, users, items = simulate(MarketConfig(seeker_arrivals=8, job_arrivals=8, days=90))
horizon = rows["day"] + 30.0
sample = extract(rows, horizon, stage=0, period=(0, 30), as_of=90,
                 mode="uniform", seed=42, sample_size=5)
print(sample["population"], rows[sample["indices"]][["user", "item", "day"]])
```

`simulate` は構造化配列 `rows` と、ユーザ・求人の属性配列を返します。`rows["user"]` と `rows["item"]` は属性配列の行インデックスです。`rows["day"]` は紹介日、`application`、`screening`、`decision` は各到達日で、未観測なら `NaN` です。

## 学習用データと評価用データを取り分ける

`extract` の `horizon` は行ごとの判定期限を並べた配列です。時刻はすべて市場の開始日を0とする日数です。`as_of` までに `horizon` に達した行が抽出対象です。未観測の結果を負例に含めないよう、`as_of` はシミュレーションの終了境界 `MarketConfig.days` 以下に設定します。`period=(start, end)` は開始を含み、終了を含みません。

| `stage` | 抽出対象となる分母 | `period` で絞る日時 |
| --- | --- | --- |
| `0` | 紹介 | 紹介日 |
| `1` | 期限内に応募（応募済み行） | 応募日 |
| `2` | 期限内に書類通過（通過済み行） | 通過日 |

`stage` が1以上のとき、分母は前段に到達した行に自動的に絞り込まれます。たとえば書類通過の抽出では、応募済みの行だけが対象になります。

- 校正と評価では `mode="uniform"` で `sample_size` を指定します。抽出された `indices` と対象件数 `population` が返ります。正負を分けずに抽出するため、少数クラスの件数は標本ごとに変わります。
- 学習では `mode="matched"` で `positive_count`（正の整数または `"all"`）と `negative_ratio`（正例1件につける負例数）を指定できます。応募の負例は同じ求職者・同じ紹介日から、通過と決定の負例は該当する分母全体から選びます。正例1件の中では負例を重複させず、別の正例間で再利用できます。`matches` には正例と負例の行インデックスの組が入ります。
- 指定数を取れない場合は `SamplingError` が発生します。`positive_eligibility="has_matched_negatives"` を指定すると、負例が必要数ある正例に限定でき、除外した正例数は `excluded_positives` に入ります。
