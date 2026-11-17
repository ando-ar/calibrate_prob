# 開発時の確認

`calibration_pipeline/` で実行します。

```powershell
uv sync --locked --extra redshift
uv run --locked --extra redshift ruff check .
uv run --locked --extra redshift ty check --error-on-warning
uv run --locked --extra redshift python -m unittest discover -s tests -v
```

[SQLiteで学習と推論を試す](../examples/sqlite/README.md) では、合成データから実際のCLIを通して確認できます。今回の実装以前の成果物は、もとのrunディレクトリで参照できます。
