# ドキュメントの更新と自動ビルド

## ローカルビルド

`calibration_pipeline/` で実行します。DB接続や認証情報は不要です。

```powershell
uv sync --locked --group docs
uv run --locked --group docs python dev/build_docs.py
```

`docs/html/index.html` をブラウザーで開いて確認します。HTMLとCSS・JavaScriptもGitに保存するため、cloneした人はビルドせずに読めます。
ビルドスクリプトはAPIページを生成し直し、警告なしで成功した場合だけ `docs/html/` を置き換えます。
`-W` は警告を失敗として扱い、APIのimport失敗や壊れた内部参照を検出します。
依存関係は `uv.lock` で固定し、実際のパッケージをimportしてAPIを生成します。

## 編集中の自動再ビルド

```powershell
uv run --locked --group docs sphinx-autobuild -E -a -W --keep-going --watch src --watch examples --ignore "*/_generated/*" docs docs/_build/html
```

表示されたローカルURLをブラウザーで開きます。MarkdownだけでなくPythonソースやSQL例の保存でも再ビルドします。
生成APIページは監視から除外し、再生成のループを防ぎます。停止はCtrl+Cです。

## CI

GitHub Actionsの `Documentation` はpush・pull request・手動実行でビルドします。
ソースや設定だけの変更でも実行するため、APIの変更も追従します。
成功したHTMLは `calibration-pipeline-docs` artifactから取得できます（保存期間14日）。
mainへのpush・mainでの手動実行では、更新されたHTMLをbotがmainへコミットします。PRではビルドのみ行います。
Webサイトには公開しません。mainへのbotのpushがブランチ保護で禁止されている場合は、ローカルで再生成した `docs/html/` をPRに含めてください。

## 更新する場所

- 利用方法は `docs/*.md`、APIの説明はソースのdocstringを編集します。
- 引数・型注釈は実装から取得します。説明のない引数や振る舞いを自動推測する仕組みではありません。
- `docs/html/` は閲覧用の生成物としてGitに含めます。手で編集せず再生成してください。`docs/_generated/` と `docs/_build/` はGitには含めません。
- SQL・設定例は `literalinclude` で実ファイルを掲載します。
- 公開モジュールの追加は自動検出されます。削除・改名時は `python dev/build_docs.py` で古いAPIページも除去できます。プレビュー用の `docs/_build/` は必要に応じて削除してください。

自動生成にはSphinx標準の [apidoc](https://www.sphinx-doc.org/en/master/usage/extensions/apidoc.html) と
[autodoc](https://www.sphinx-doc.org/en/master/usage/extensions/autodoc.html) を使います。

