"""Read-only extraction into a persistent, typed local snapshot."""
from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from .config import Config, identifier, table_identifier
from .io import file_hash, identity, read_json, write_json


def datetime_series(series: pd.Series, timezone: str) -> pd.Series:
    values = pd.to_datetime(series, errors="raise", format="mixed")
    if values.dt.tz is None:
        values = values.dt.tz_localize(timezone, ambiguous="raise", nonexistent="raise")
    return values.dt.tz_convert("UTC").dt.tz_localize(None).astype("datetime64[us]")


def normalize(frame: pd.DataFrame, config: Config, *, inference: bool = False) -> pd.DataFrame:
    source = config.section("source")
    labels = config.section("labels")
    features = config.section("features")
    timezone = source.get("timezone", "UTC")
    frame = frame.copy()
    required = set(config.columns)
    if inference:
        required -= {labels[key] for key in ("application_column", "screen_column", "target_column")}
        required.discard(labels.get("fee_column"))
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"Missing source columns: {sorted(missing)}")
    frame = frame[[column for column in config.columns if column in frame]].copy()
    for key in ("user_column", "item_column"):
        name = source[key]
        if frame[name].isna().any():
            raise ValueError(f"{name} cannot be NULL")
        frame[name] = frame[name].astype("string")
    date_columns = [source["anchor_column"]]
    if not inference:
        date_columns += [labels[key] for key in ("application_column", "screen_column", "target_column")]
    for name in dict.fromkeys(date_columns):
        frame[name] = datetime_series(frame[name], timezone)
    anchor = frame[source["anchor_column"]]
    if anchor.isna().any():
        raise ValueError("The anchor column cannot be NULL, including for non-applications")
    for name in features["categorical"]:
        frame[name] = frame[name].astype("string")
    numeric = list(features["numeric"])
    for proxy in config.values["proxies"]:
        numeric.extend(proxy[key] for key in ("lower_column", "upper_column", "desired_column", "rate_column")
                       if proxy.get(key))
    if not inference and labels.get("fee_column"):
        numeric.append(labels["fee_column"])
    for name in dict.fromkeys(numeric):
        frame[name] = pd.to_numeric(frame[name], errors="raise").astype("float64")
    row_id = source.get("row_id_column")
    if row_id:
        if frame[row_id].isna().any():
            raise ValueError("row_id_column cannot contain NULL")
        frame["_row_id"] = frame[row_id].astype("string")
    else:
        keys = zip(frame[source["user_column"]], frame[source["item_column"]], anchor.astype(str))
        frame["_row_id"] = pd.Series(
            [hashlib.sha256(json.dumps(list(key), ensure_ascii=False).encode()).hexdigest() for key in keys],
            index=frame.index, dtype="string",
        )
    frame["_anchor"] = anchor
    frame["_day"] = anchor.dt.tz_localize("UTC").dt.tz_convert(timezone).dt.tz_localize(None).dt.normalize()
    frame["_user"] = frame[source["user_column"]]
    frame["_item"] = frame[source["item_column"]]
    return frame


def extraction_sql(config: Config) -> tuple[str, tuple]:
    source = config.section("source")
    anchor = identifier(source["anchor_column"])
    columns = ", ".join(identifier(column) for column in config.columns)
    # Convert boundaries back to the timezone of the warehouse's naive timestamps.
    timezone = source.get("timezone", "UTC")
    boundaries = [config.date("split", key).tz_localize("UTC").tz_convert(timezone).tz_localize(None).to_pydatetime()
                  for key in ("train_start", "test_end")]
    sql = f"SELECT {columns} FROM {table_identifier(source['table'])} WHERE {anchor} >= %s AND {anchor} < %s"
    return sql, tuple(boundaries)


@contextmanager
def redshift_connection(config: Config):
    import redshift_connector

    settings = config.section("source").get("connection", {})
    mapping = {"host": "REDSHIFT_HOST", "database": "REDSHIFT_DATABASE", "user": "REDSHIFT_USER",
               "password": "REDSHIFT_PASSWORD"}
    kwargs = {}
    for name, default in mapping.items():
        variable = settings.get(name + "_env", default)
        if not os.environ.get(variable):
            raise ValueError(f"Required environment variable is unset: {variable}")
        kwargs[name] = os.environ[variable]
    kwargs.update(port=int(settings.get("port", 5439)), ssl=True, sslmode="verify-full",
                  timeout=int(settings.get("timeout_seconds", 60)))
    connection = redshift_connector.connect(**kwargs)
    try:
        yield connection
    finally:
        try:
            connection.rollback()
        finally:
            connection.close()


def source_batches(config: Config):
    source = config.section("source")
    chunk_rows = source.get("chunk_rows", 50000)
    if source["kind"] == "file":
        path = config.resolve(source["path"])
        if path.suffix.lower() == ".csv":
            yield from pd.read_csv(path, chunksize=chunk_rows, dtype="string")
        else:
            parquet = pq.ParquetFile(path)
            for batch in parquet.iter_batches(batch_size=chunk_rows, columns=config.columns):
                yield batch.to_pandas()
        return
    sql, params = extraction_sql(config)
    with redshift_connection(config) as connection:
        cursor = connection.cursor()
        try:
            cursor.paramstyle = "format"
            cursor.execute(sql, params)
            names = [column[0] for column in cursor.description]
            while rows := cursor.fetchmany(chunk_rows):
                yield pd.DataFrame(rows, columns=names)
        finally:
            cursor.close()


def snapshot(config: Config) -> Path:
    """A completed snapshot is reused. An interrupted extraction restarts atomically."""
    folder = config.output / "snapshot"
    folder.mkdir(parents=True, exist_ok=True)
    output, receipt = folder / "rows.parquet", folder / "manifest.json"
    source = dict(config.section("source"))
    if source["kind"] == "file":
        source["file_sha256"] = file_hash(config.resolve(source["path"]))
    request = {"source": source, "columns": config.columns, "features": config.section("features"),
               "labels": config.section("labels"), "split": config.section("split"),
               "feature_snapshot_mode": "latest"}
    request_hash = identity(request)
    if receipt.exists():
        saved = read_json(receipt)
        if saved["request_sha256"] != request_hash:
            raise ValueError("Snapshot configuration changed. Choose a new output_dir")
        if not output.exists() or file_hash(output) != saved["file_sha256"]:
            raise ValueError("The cached snapshot is incomplete or changed")
        return output
    with NamedTemporaryFile(dir=folder, suffix=".parquet", delete=False) as temp:
        temporary = Path(temp.name)
    writer = None
    count = 0
    try:
        for raw in source_batches(config):
            frame = normalize(raw, config)
            frame = frame[(frame["_anchor"] >= config.date("split", "train_start")) &
                          (frame["_anchor"] < config.date("split", "test_end"))]
            if frame.empty:
                continue
            table = pa.Table.from_pandas(frame, preserve_index=False)
            if writer is None:
                writer = pq.ParquetWriter(temporary, table.schema, compression="zstd")
            writer.write_table(table)
            count += len(frame)
        if writer is None:
            raise ValueError("The extraction returned no rows in the configured split interval")
        writer.close()
        writer = None
        with duckdb.connect() as db:
            total, unique = db.execute("SELECT count(*), count(DISTINCT _row_id) FROM read_parquet(?)",
                                       [str(temporary)]).fetchone()
        if total != unique:
            raise ValueError("Duplicate row IDs. Supply an immutable row_id_column for repeated pair events")
        os.replace(temporary, output)
        write_json(receipt, {"request_sha256": request_hash, "request": request,
                             "file_sha256": file_hash(output), "rows": count,
                             "created_at_utc": pd.Timestamp.now(tz="UTC").isoformat(),
                             "latest_feature_leakage_accepted": True})
    finally:
        if writer is not None:
            writer.close()
        temporary.unlink(missing_ok=True)
    return output
