# competition1

合成データを用いた期待手数料最大化コンペ（仕様: `synthetic_funnel/docs/COMPETITION.md`）。

## 構成

| ディレクトリ | 内容 |
| --- | --- |
| `participants/` | 参加者に配布・公開する成果物（データの入手方法・提出形式・参加者向け README） |
| `organizer/` | 運営側の成果物（[`RULES.md`](organizer/RULES.md)、[`frozen_config.json`](organizer/frozen_config.json)、[`build_release.py`](organizer/build_release.py)） |

- 潜在テーブル（`user_latent` / `item_latent` / `events_latent` / `candidates_latent`）は
  運営専用で `participants/` に置かない（配布前に `build_release.py` が除去し、アサートする）。
- 実験記録は `experiments/competition1/` に従う（`experiments/_template/` の規約）。
- 生成物（sqlite/parquet）は `.gitignore` 済み。生成手順は
  [`organizer/README.md`](organizer/README.md) を参照。
