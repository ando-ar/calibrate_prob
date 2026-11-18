# ドキュメントの更新と自動ビルド

## ローカルビルド

`calibration_pipeline/` で実行します。DB接続や認証情報は不要です。

```powershell
uv sync --locked --group docs
uv run --locked --group docs python dev/build_docs.py
```

`docs/html/index.html` をブラウザーで開いて確認します。出力先の `docs/html/` はGitの追跡対象外で、再生成してもGitの差分には出ません。
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
すべての実行でSphinxの警告をエラーとして検証し、成功したHTML一式を `calibration-pipeline-docs` artifactに保存します（保持期間14日）。
ワークフローのリポジトリ権限は `contents: read` で、生成物のコミット・pushは行いません。Webサイトには公開しません。

### Actionsから取得して閲覧する

1. GitHubにログインし、リポジトリの [Actions → Documentation](https://github.com/ando-ar/calibrate_prob/actions/workflows/docs.yml) を開きます。
2. 閲覧したいブランチ・コミットの成功した実行を選びます。mainの内容を読む場合はmainの実行を選びます。
3. 実行の概要にある **Artifacts** から `calibration-pipeline-docs` をダウンロードします。
4. ZIPをフォルダーへすべて展開し、その直下の `index.html` をブラウザーで開きます。`_static/` なども含め、展開したファイルの配置を保ってください。HTMLだけを取り出すとスタイルやリンクが機能しません。

取得後はPython環境やWebサーバーなしでオフライン閲覧できます。artifactの保持期間は14日です。期限切れの場合は、実行権限があれば **Run workflow** で対象ブランチを指定して再生成するか、上記のローカルビルドを利用してください。
デモGUI内の利用ガイドへのリンクを使う場合は、ローカルビルドするか、artifactの中身を `calibration_pipeline/docs/html/` に展開してください。

## 更新する場所

- 利用方法は `docs/*.md`、APIの説明はソースのdocstringを編集します。
- 引数・型注釈は実装から取得します。説明のない引数や振る舞いを自動推測する仕組みではありません。
- `docs/html/`、`docs/_generated/`、`docs/_build/` は生成物としてGit管理から除外します。手で編集せず再生成し、PRには原稿・docstring・設定の変更を含めてください。
- SQL・設定例は `literalinclude` で実ファイルを掲載します。
- 公開モジュールの追加は自動検出されます。削除・改名時は `python dev/build_docs.py` で古いAPIページも除去できます。プレビュー用の `docs/_build/` は必要に応じて削除してください。

自動生成にはSphinx標準の [apidoc](https://www.sphinx-doc.org/en/master/usage/extensions/apidoc.html) と
[autodoc](https://www.sphinx-doc.org/en/master/usage/extensions/autodoc.html) を使います。

