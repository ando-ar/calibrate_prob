# competition1

合成データを用いた期待手数料最大化コンペ（仕様: `synthetic_funnel/docs/COMPETITION.md`）。

## 構成

| ディレクトリ | 内容 |
| --- | --- |
| `participants/` | 参加者に配布・公開する成果物（データの入手方法・提出形式・参加者向けREADME） |
| `organizer/` | 運営側の成果物（ルール確定書類、市場config、評価手順、実験記録） |

- 潜在テーブル（`user_latent` / `item_latent` / `events_latent` / `candidates_latent`）は運営専用で `participants/` に置かない
- 実験記録は `experiments/` の慣習（`experiments/_template/`）に従う
