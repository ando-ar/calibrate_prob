# GitHub Spec Kit 導入の検討（提案・草案）

> ステータス: **草案**（調査結果と提案の整理。本リポジトリへの Spec Kit 導入は行っていない）。
> 対象: ando-ar/calibrate_prob。既存の Issue ベース開発・Orca コーディネーター/ワーカー・契約ドキュメント群との整合を検討する。
> 位置づけ: 本ドキュメントは提案のみを扱い、コード・依存関係・CI は変更しない。採用可否はユーザー判断（§7、§9）。

## 0. 目的

GitHub の Spec Kit（`github/spec-kit`）を本プロジェクトへ適用できるか、その導入形態（不採用／部分採用／全面採用）を提案する。本プロジェクトはすでに「契約をドキュメントとして定め、Issue とコメントでの Q&A（grilling）で確定し、Orca コーディネーターがワーカーを worktree へ派遣して実装する」という、人手による Spec-Driven Development（SDD）に近い運用を確立している。Spec Kit はこれをツール化・テンプレート化したものであり、重複と摩擦を最小化する導入形態を探るのが本ドキュメントの目的である。

## 1. Spec Kit とは何か

Spec Kit は GitHub が公開しているオープンソース（MIT）のツールキットで、AI コーディングエージェントに「構造化されたプロセス・再利用可能なテンプレート・文書化された成果物」を与える。仕様を起点に計画・タスク・実装・収束までを進める Spec-Driven Development（SDD）が中核で、バグ修正・アイデア評価のプロセスも拡張として同梱される。

- リポジトリ: https://github.com/github/spec-kit （約 15.1 万 star / 13.6 万 fork、2,500 コミット、活発に開発中）
- ドキュメント: https://github.github.io/spec-kit/
- 前提: Python 3.11+、`uv`、対応するコーディングエージェント（Linux/macOS/Windows）
- インストール: `uv tool install specify-cli` → `specify init <project> --integration <key>`

Spec Kit は「`specify` CLI（初期化・統合・拡張・プリセット・ワークフローの管理）」と「エージェントのチャット内で実行するプロセスコマンド（スキル）」の二層からなる。プロセスコマンドはターミナルではなくエージェントのチャットで 1 つずつ実行し、各結果を確認しながら進める設計である。

### 1.1 主なプロセスコマンド（SDD）

| コマンド | 役割 |
| --- | --- |
| `/speckit.constitution` | プロジェクトの原則（constitution）を確立・更新（各フェーズの評価基準） |
| `/speckit.specify` | 自然言語から仕様（what/why）を生成 |
| `/speckit.clarify` | 仕様の曖昧な箇所を最大5問で解消（旧 `/quizme`） |
| `/speckit.plan` | 技術実装計画（tech stack・制約）を生成 |
| `/speckit.checklist` | 要件品質のチェックリスト生成（「要件のユニットテスト」） |
| `/speckit.tasks` | plan を依存順のタスクへ分解（Setup→Foundational→ユーザーストーリー別フェーズ→Polish） |
| `/speckit.analyze` | spec/plan/tasks の整合性を読み取り専用で検査（報告のみ、編集しない） |
| `/speckit.implement` | tasks.md を依存順に実装（並列マーカー尊重、段階実行も可） |
| `/speckit.converge` | 実装を仕様・計画・タスクと突き合わせ、残作業を tasks.md に追記（append-only） |

標準フローは `constitution → specify → clarify → plan → checklist → tasks → analyze → implement → converge` で、`implement`/`converge` を「Converged」になるまで繰り返す。`specify` と `plan` 以外は省略可能な品質ゲートである。

### 1.2 成果物（ディレクトリ構成）

`specify init` が `.specify/` と選択した統合のスキル／コマンドを生成する。中核は次のとおり。

```text
.specify/
  memory/constitution.md          # プロジェクト原則
  templates/                      # spec/plan/tasks/checklist のテンプレート
specs/<feature>/
  spec.md                         # 仕様（what/why）
  plan.md                         # 技術計画（how）
  tasks.md                        # 依存順タスク
  checklists/requirements.md      # 要件品質チェックリスト
```

### 1.3 統合（エージェント対応）

40 以上のエージェントをサポートする。本タスクで想定されるものはすべて公式に対応する。

| エージェント | 統合キー | 呼び出し形式 |
| --- | --- | --- |
| Claude Code | `claude` | スキル形式（`/speckit-*` 等） |
| Codex CLI | `codex` | `$speckit-specify` 等 |
| opencode | `opencode` | `/speckit.specify`（`.opencode/commands/` に配置） |
| GitHub Copilot | `copilot` | `/speckit-specify`（既定） |

チームで複数エージェントを使う場合は `specify init --integration none` で中立即の共有基盤を作り、各自が `specify integration switch <key>` で切り替える運用が案内されている。

### 1.4 既存プロジェクトへの適用（brownfield）

Spec Kit は greenfield 専用ではない。既存リポジトリでは `specify init --here --force --integration <key>` で初期化し、`/speckit-constitution` に README・ADR・CONTRIBUTING・CI を根拠とした原則を「すでに真である」ものだけ書く。既存コードを遡って仕様化せず、次の bounded な変更だけを仕様化する手順が公式ガイドにある（§10 参照）。

## 2. このプロジェクトの現状のワークフロー

本リポジトリは次の運用を確立している（`README.md`「Issue ベースの開発」、`synthetic_funnel/docs/COMPETITION.md`、`competitions/competition1/organizer/RULES.md` 等）。

1. 作業前に GitHub Issue を作り、目的・変更範囲・完了条件を記載（`.github/ISSUE_TEMPLATE/task.md`）。
2. `issue-<番号>-<短い説明>` ブランチ（worktree）で実装。Orca コーディネーターがワーカーエージェントを各 worktree へ派遣する。
3. 契約はドキュメントとして確定する: `synthetic_funnel/docs/COMPETITION.md`（コンペ仕様）、`DATA_FORMAT.md`（データ契約）、`frozen_config.json`（凍結 config）、`competitions/competition1/organizer/RULES.md`（運営ルール）。仕様の未決点は Issue コメントの Q&A（grilling）で潰し、確定内容を文書へ反映する（`COMPETITION.md` §11 未決点、`issue-32-observation-design.md` の「確定版」）。
4. 各ディレクトリの `AGENTS.md` と `.githooks/pre-commit`（Ruff・型検査）が検査を課し、`Closes #<番号>` で Issue と PR を関連付ける。

つまり本プロジェクトは「仕様を先行させ、人間のレビューで確定し、分割実装し、検査で収束する」SDD の本質を、ツールなしで運用している状態である。

## 3. Spec Kit 概念と既存要素の対応表

| Spec Kit の概念 | 本リポジトリの既存要素 | 重なり／摩擦 |
| --- | --- | --- |
| constitution（不変の原則） | `README.md`「Issue ベースの開発」「保存するもの」、各 `AGENTS.md`、`.githooks`、`RULES.md`、`COMPETITION_LESSONS.md` | 原則は明文化済みだが一箇所に集約されていない。constitution として 1 ファイルにまとめる価値はある |
| spec（仕様 = what/why） | `COMPETITION.md`、`DATA_FORMAT.md`、`MARKET.md`、`frozen_config.json`、`RULES.md` | 既に高品質な契約仕様が存在し、spec-kit の `spec.md` と役割が重なる |
| clarify（曖昧さ解消） | Issue コメントの Q&A（grilling）、`COMPETITION.md` §11 未決点 | spec-kit は agent が最大5問投げる自動化。本プロジェクトは人間（ユーザー）が確定する手順が確立しており、自動化への置換は逆に回り道になり得る |
| plan（技術実装計画 = how） | Issue 本文の「変更範囲」、`drafts/issue-32-observation-design.md` | 設計ドキュメントの慣習（`docs/drafts/`）が既にある。`plan.md` テンプレートは参考にできる |
| tasks（依存順タスク分解） | Issue → 実装ブランチ → PR の分解、`Closes #N` | フェーズ分け・依存順・並列可否の形式化は現状弱い。タスク分解テンプレートは有用 |
| checklist / analyze（整合性検査） | `AGENTS.md` の検査、`.githooks`（Ruff/型）、`pytest`、PR レビュー | 現状はコード品質・テストに偏り、要件⇔実装の整合性検査は人手。checklist/analyze の考え方は取り込める |
| implement（実装） | Orca コーディネーター → ワーカーが worktree で実装 | spec-kit は単一エージェントの対話チャットで 1 コマンドずつ進める前提。ヘッドレスでワーカーを派遣する Orca 運用とは構造が異なる |
| converge（収束確認） | PR レビュー、`Closes #N`、再現性検証（`run_metadata.json`） | append-only で残作業を tasks に追記する形式化はない。収束チェックの仕組みは有用 |

**総括**: 「何を作るか」の仕様化・確定・分割・検査はすでに運用として存在する。Spec Kit が追加で提供する価値は (1) 原則の一元化（constitution）、(2) spec/plan/tasks の標準テンプレート、(3) 整合性検査（analyze）と収束（converge）の形式化、であり、プロセスそのものの置換ではない。

## 4. 導入オプション

### 案 a: 不採用（現状維持）

Spec Kit を導入しない。現行の Issue ベース開発と契約ドキュメント運用を続ける。

- 利点: 追加のツール・成果物・学習コストなし。既存フローに一切の摩擦がない。
- 欠点: 原則の一元化・タスクの依存順分解・整合性検査・収束確認が引き続き人手任せで、機能が大きくなるほど品質がレビュアー／コーディネーターの裁量に依存する。

### 案 b: 部分採用（Spec Kit の「成果物テンプレート」だけを借りる）

Spec Kit を恒久導入せず、`specify` CLI が生成するテンプレート（constitution / spec / plan / tasks / checklist）の構造を、本リポジトリの日本語ドキュメント様式へ移植して `docs/` に置く。Issue 駆動フローは維持し、将来の大きな機能ではこのテンプレートを仕様ドキュメントの起点にする。

- 利点: 既存の Issue 駆動・Orca 派遣を壊さず、SDD の「型」だけを取り込める。トークン消費・二重管理のリスクが小さい。
- 欠点: agent による自動生成・`analyze`/`converge` の自動検査は得られない。テンプレートが形骸化しないよう運用規約が必要。

### 案 c: 全面採用（`specify` CLI を新規作業の入り口にする）

`specify init --here --force --integration opencode` で本リポジトリを初期化し、新規機能は `/speckit.specify` から開始する。Issue は Spec Kit 成果物（`.specify/`・`specs/`）へ置き換える。

- 利点: SDD の全工程（自動 clarify・analyze・converge）を享受でき、複数エージェント（Claude Code / Codex / opencode）へ標準手順として展開できる。
- 欠点: ① 現行の Issue 駆動・grilling 確定フローとの二重管理が発生し、issue と spec.md/plan.md/tasks.md の同期コストが増える。② ワーカーを worktree へヘッドレス派遣する Orca 運用では、単一エージェントの対話チャット前提のコマンド実行をどう組み込むか設計が必要。③ 成果物テキストが膨らみ、本タスクが戒めるトークン消費を圧迫する（§5 参照）。④ opencode 統合には ripgrep が `.specify/**` を隠す既知の癖があり `.ignore` 例外の追加が必要（§10 参照）。

## 5. 推奨案と根拠

**推奨: 案 b（部分採用）**。

理由は次のとおり。

1. **本質は既に運用済み**: 契約仕様（`COMPETITION.md`/`DATA_FORMAT.md`）と grilling による確定、Issue 分解、`AGENTS.md`/`.githooks`/`pytest` による検査は、Spec Kit が自動化しようとする SDD を人手で実現している。全面採用は既存資産の二重化にしかならない。
2. **Orca 運用との構造ミスマッチ**: Spec Kit のプロセスコマンドは「単一エージェントが対話チャットで 1 つずつ実行し、都度結果をレビューする」ことを前提とする。コーディネーターがワーカーを worktree へ派遣する本運用では、この対話ループを再現するための接着層が別途必要になり、導入コストに見合わない。
3. **トークン・時間コスト**: Spec Kit は仕様・計画・タスク・チェックリストを多量に生成する。コミュニティでも「仕事をしている錯覚を生み、テキストを大量生成する」という指摘がある（§10 参照）。本プロジェクトはすでに高密度な日本語仕様を保持しており、冗長な中間成果物は価値が薄い。
4. **テンプレートの型だけは有用**: 一方で「原則の一元化（constitution）」「spec/plan/tasks の分離」「依存順タスク分解」「収束（converge）の形式化」は、大きな機能ほど欠落しやすい。これらをドキュメントの「型」として軽量に借用すれば、追加ツールなしで品質を底上げできる。

## 6. 部分採用のパイロット計画（案 b を採る場合）

Spec Kit を恒久導入せず、テンプレートだけを移植した上で、小さな次期機能 1 件をパイロットにする。

**準備（1 回）**
- TEMP ディレクトリに `uv tool install specify-cli` → `specify init --here --force --integration none` を実行し、`.specify/templates/`（spec/plan/tasks/checklist/constitution）を取得して内容を確認する。生成物は TEMP に閉じ、リポジトリへはコピーしない（恒久導入しない）。
- 取得したテンプレートの構造を、本リポジトリの様式（日本語・確定版ヘッダ・表・「参照」節）へ移植し、`docs/drafts/` に「機能仕様テンプレート」として置く（例: `docs/drafts/spec-template.md` 等）。

**パイロット（次期の小さな機能 1 件）**
- 対象: bounded で独立にレビューできる小機能（例: `frozen_config.json` が将来課題とする occupation/region 別 partition の追加、または既存仕様の小改修）。既存 Issue を親として維持する。
- 使うコマンド（相当）: 自動 CLI ではなく、テンプレートに沿って人間（＋agent）が `spec.md` → `plan.md` → `tasks.md` を記述。`clarify` 相当は従来どおり Issue コメントの grilling で確定し、`COMPETITION.md` §11 のような「未決点（推奨付き）」節を spec に設ける。
- 使うエージェント: 既存の Orca ワーカー（opencode）。`implement` 相当はコーディネーターが `tasks.md` のフェーズ／依存順を参照して worktree へ分割派遣する。
- 収束（converge 相当）: 実装後に `tasks.md` の全項目が `AGENTS.md` 検査・`pytest`・レビューで閉じたことを確認し、残作業があれば tasks に追記（append-only）して「Converged」相当の状態を記録する。

**評価基準**
- 既存 Issue 駆動フローに追加コストがどの程度かかるか（二重管理にならないか）。
- 依存順タスク分解・収束確認が、従来の「大きな Issue を一気に実装」する場合と比べて品質・レビュー容易性を改善したか。
- テンプレートが形骸化せず、次の機能にも再利用できるか。

## 7. リスクと未決点（ユーザーへの質問）

1. **二重管理のリスク**: 案 b でも Issue と spec/plan/tasks の両方を保守すると同期コストが発生する。Issue 本文を薄くして spec 文書へ委ねる方針か、spec 文書は「設計の下書き」に留めて確定後は Issue へ一本化する方針か。
2. **トークン・時間コストの許容**: 本タスクはトークン消費の抑制を指示している。spec/plan/tasks の 3 層を毎機能で生成するのは過剰か。大きな機能だけに限定する、等の閾値をどこに置くか。
3. **constitution の一元化**: 現状 README・AGENTS.md・RULES.md・`.githooks` に分散する原則を `.specify/memory/constitution.md` 相当の 1 ファイルへ集約する価値があるか（Spec Kit を使わずとも、素朴な `docs/CONSTITUTION.md` として集約できる）。
4. **複数エージェント／worktree への展開**: Spec Kit のデフォルト統合は 1 リポジトリ 1 つ。Orca の複数 worktree・複数エージェント運用で `.specify/` 状態をどう共有するか（各 worktree に再生成か、共有か）。全面採用（案 c）に踏み切る場合のみ必要だが、先に方針を決めておきたい。
5. **opencode 統合の既知の癖**: opencode 1.18/1.19（darwin arm64 で確認）は ripgrep の glob が `.specify/**` を隠すため `.ignore` への `!.specify/` 例外追加が必要。Windows 環境での挙動確認と、恒久導入する場合の `.ignore` 管理方針。
6. **Windows 環境**: Spec Kit は Windows を公式サポートするが、スクリプトは bash/zsh と PowerShell で分岐する。本リポジトリは `uv` 前提で `.githooks` を持つため、PowerShell 側のスクリプト互換を確認する必要がある。
7. **採否と優先度**: 本提案はリサーチであり、ユーザーの採否判断を待つ。採る場合の優先度（constitution 集約を先にやるか、テンプレート移植を先にやるか）も確定したい。

## 8. 現時点の結論

- Spec Kit は成熟しつつあり（Spec Kit 1.0、151k star、拡張・プリセット・ワークフロー・カタログまで拡張）、brownfield 対応・opencode/Claude Code/Codex 対応も公式に揃っている。
- しかし本プロジェクトは SDD の本質を既に人手で運用しており、プロセス置換（案 c）は二重管理・トークン・Orca 派遣モデルとの摩擦を生む。
- 最適解は**部分採用（案 b）**: Spec Kit の成果物テンプレートの「型」だけを本リポジトリの様式へ移植し、将来の大きな機能の仕様ドキュメント起点に使う。まず小さな次期機能 1 件でパイロットし、評価する。
- 導入可否・優先度はユーザー判断（§7）。

## 参照

- Spec Kit リポジトリ: https://github.com/github/spec-kit
- Spec Kit ドキュメント: https://github.github.io/spec-kit/
- SDD クイックスタート: https://github.github.io/spec-kit/quickstart.html
- Agentic SDD コマンドリファレンス: https://github.github.io/spec-kit/reference/agentic-sdd.html
- 既存プロジェクトへの導入ガイド: https://github.github.io/spec-kit/how-to/existing-projects.html
- 統合（エージェント対応）一覧: https://github.github.io/spec-kit/integrations/index.html
- opencode 統合: https://github.github.io/spec-kit/integrations/opencode.html
- 統合 CLI リファレンス（呼び出し形式・管理）: https://github.github.io/spec-kit/integrations/cli.html
- 日本語 README: https://github.com/github/spec-kit/blob/main/README.ja.md
- 批判・限界の議論（成果物が膨らむ）: https://github.com/github/spec-kit/discussions/1784
- constitution 上書き問題（既知 issue）: https://github.com/github/spec-kit/issues/1541
- constitution と AGENTS.md の位置づけ議論: https://github.com/github/spec-kit/discussions/2476
