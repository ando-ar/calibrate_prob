# Python API

以下のページはビルドごとに `src/calibration_pipeline/` から生成します。
公開モジュール・関数・クラス・メソッドの追加と、引数・型注釈・docstringの変更が反映されます。
説明文はコード側のdocstringを更新してください。docstringのない公開関数もシグネチャを掲載します。

主な入口は {py:func}`calibration_pipeline.pipeline.train` と
{py:func}`calibration_pipeline.pipeline.inference` です。
SQLの実行は {py:class}`calibration_pipeline.source.Source`、設定は
{py:class}`calibration_pipeline.config.Config` を参照してください。

```{toctree}
:maxdepth: 2

_generated/modules
```

