# competition1 運営側（organizer）

確定ルールは [`RULES.md`](RULES.md)、凍結 config は [`frozen_config.json`](frozen_config.json)、
配布パッケージの生成は [`build_release.py`](build_release.py) が担う。

| ファイル | 内容 |
| --- | --- |
| `RULES.md` | 凍結 config・評価プロトコル・提出ルール・禁止事項・ラウンド |
| `frozen_config.json` | 凍結された本番 config（market + competition + baseline + evaluation seeds） |
| `build_release.py` | データ生成 → participant パッケージ化 → ベースライン → 評価 → metrics/manifest 記録 |
| `data/` | 生成物（sqlite/parquet）。gitignore 済み・評価後に削除 |
| `metrics/` | ベースライン指標と data version（manifest ハッシュ）。コミットして残す |

## 生成手順

```powershell
# 本番（200/200 × 280日 × 5 seed、逐次実行して DB/parquet は都度削除）
uv run --project ../../../synthetic_funnel python build_release.py --seeds 71,72,73,74,75

# 検証（--proof: 50/50 × 1 seed、全パイプラインを数分で確認）
uv run --project ../../../synthetic_funnel python build_release.py --proof
```

- data version（配布 `manifest.json` の SHA-256）は `metrics/data_versions.json` と
  `experiments/competition1/` の `run_metadata.json` に記録される。
- 潜在テーブル（`user_latent` / `item_latent` / `events_latent` / `candidates_latent`）は
  運営専用。participant パッケージから除去済みであることをスクリプトがアサートする。
- 空き約 17GB のため 5 seed を同時保持しない（RULES.md §2.4）。
