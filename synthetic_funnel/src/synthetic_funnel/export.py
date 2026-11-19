"""Persist one stream of observed and oracle rows in a selected format."""

import csv
import sqlite3
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from tempfile import TemporaryDirectory

FORMATS = ("sqlite", "parquet", "csv")
SQLITE_FILENAME = "dataset.sqlite.db"
# 16,384 rows is the measured sweet spot for both sqlite executemany and parquet
# row groups at 50/50 arrivals; 65,536 was slightly slower for both (issue #40).
BATCH_SIZE = 16384
TEXT_FIELDS = {
    "event_id", "user_id", "item_id", "failure_stage", "failure_reason",
    "shoukai_dt", "oubo_dt", "shorui_tsuuka_dt", "kettei_dt",
    "latent_oubo_dt", "latent_shorui_tsuuka_dt", "latent_kettei_dt", "arrival_dt", "day_dt",
    "departure_dt", "occupation", "region",
    "status", "outcome_dt",
}
INTEGER_FIELDS = {
    "wants_remote", "allows_remote", "remote_mismatch", "other_applications", "competing_candidates",
    "referrals",
    "live_users", "live_items", "new_users", "new_items", "applications", "screenings", "decisions",
    "auto",
}
# Created on every sqlite export so both the organiser DB and any copy of it
# inherit the lookups used by the daily-candidate recipe.  The correlated
# subqueries in market_candidates.DAILY_CANDIDATES_LATENT_SQL probe events by
# (user_id, item_id) and by user_id/item_id with date predicates; without these
# indexes each candidate scans the whole events table (issue #38).
SQLITE_INDEXES = (
    "CREATE INDEX events_shoukai_dt ON events (shoukai_dt)",
    "CREATE INDEX idx_events_user ON events (user_id, oubo_dt, kettei_dt, outcome_dt)",
    "CREATE INDEX idx_events_item ON events (item_id, oubo_dt, kettei_dt, outcome_dt)",
    "CREATE INDEX idx_events_user_item ON events (user_id, item_id)",
    "CREATE INDEX idx_user_occupation ON user (occupation, arrival_dt, departure_dt)",
    "CREATE INDEX idx_item_occupation ON item (occupation, arrival_dt, departure_dt)",
)


@dataclass(frozen=True)
class Table:
    name: str
    fields: tuple[str, ...]
    key: str
    foreign_keys: tuple[tuple[str, str, str], ...] = ()


@contextmanager
def dataset_staging(output, output_format):
    if output_format not in FORMATS:
        raise ValueError(f"output_format must be one of: {', '.join(FORMATS)}")
    output = Path(output)
    if output.exists():
        raise FileExistsError(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".synthetic-funnel-", dir=output.parent) as temporary:
        staging = Path(temporary) / "dataset"
        staging.mkdir()
        yield staging
        if output.exists():
            raise FileExistsError(output)
        staging.rename(output)


def field_type(name):
    if name in TEXT_FIELDS:
        return "TEXT"
    if name in INTEGER_FIELDS:
        return "INTEGER"
    return "REAL"


def typed_rows(rows, fields):
    types = {"TEXT": str, "INTEGER": int, "REAL": float}
    converters = [types[field_type(name)] for name in fields]
    return [
        {name: None if (value := row.get(name)) in ("", None) else convert(value)
         for name, convert in zip(fields, converters, strict=True)}
        for row in rows
    ]


def batches(rows):
    rows = iter(rows)
    while batch := list(islice(rows, BATCH_SIZE)):
        yield batch


def write_csv(output, tables, records):
    with ExitStack() as stack:
        writers = {}
        for table in tables:
            stream = stack.enter_context((output / f"{table.name}.csv").open("x", encoding="utf-8", newline=""))
            writer = csv.DictWriter(stream, fieldnames=table.fields)
            writer.writeheader()
            writers[table.name] = writer
        for table, rows in records:
            writers[table.name].writerows(rows)


def write_sqlite(output, tables, records):
    # The database is built in a staging directory that is renamed into place
    # only after this function returns, so a crash can never publish a partial
    # file.  Skipping rollback journalling/fsync and caching more pages therefore
    # changes only write speed, not the published bytes (issue #40).
    with closing(sqlite3.connect(output / SQLITE_FILENAME)) as database, database:
        database.execute("PRAGMA journal_mode = OFF")
        database.execute("PRAGMA synchronous = OFF")
        database.execute("PRAGMA temp_store = MEMORY")
        database.execute("PRAGMA cache_size = -65536")
        database.execute("PRAGMA foreign_keys = ON")
        insert_statements = {}
        for table in tables:
            definitions = [
                f"{name} {field_type(name)}" + (" NOT NULL PRIMARY KEY" if name == table.key else "")
                for name in table.fields
            ]
            definitions.extend(
                f"FOREIGN KEY ({column}) REFERENCES {target} ({target_key})"
                for column, target, target_key in table.foreign_keys
            )
            database.execute(f"CREATE TABLE {table.name} ({', '.join(definitions)})")
            columns = ", ".join(table.fields)
            placeholders = ", ".join(f":{name}" for name in table.fields)
            insert_statements[table.name] = f"INSERT INTO {table.name} ({columns}) VALUES ({placeholders})"
        for table, rows in records:
            database.executemany(insert_statements[table.name], typed_rows(rows, table.fields))
        # Indexes are created only after every row is inserted: maintaining the
        # six lookups per row is ~31% slower on a 50/50 x 180-day dataset.
        for statement in SQLITE_INDEXES:
            database.execute(statement)


def write_parquet(output, tables, records):
    import pyarrow as pa
    from pyarrow.parquet import ParquetWriter

    types = {"TEXT": pa.string(), "INTEGER": pa.int64(), "REAL": pa.float64()}
    with ExitStack() as stack:
        writers = {}
        for table in tables:
            schema = pa.schema([pa.field(field, types[field_type(field)], nullable=field != table.key)
                                for field in table.fields])
            writer = stack.enter_context(ParquetWriter(output / f"{table.name}.parquet", schema))
            writers[table.name] = writer, schema
        for table, rows in records:
            writer, schema = writers[table.name]
            writer.write_table(pa.Table.from_pylist(typed_rows(rows, table.fields), schema=schema))


def write_rows(output, output_format, tables, records, *, extra_tables=()):
    def table_batches():
        for table, rows in extra_tables:
            for batch in batches(rows):
                yield table, batch
        for batch in batches(records):
            yield from zip(tables, zip(*batch, strict=True), strict=True)

    all_tables = (*(table for table, _ in extra_tables), *tables)
    writers = {"sqlite": write_sqlite, "parquet": write_parquet, "csv": write_csv}
    writers[output_format](output, all_tables, table_batches())
    if output_format == "sqlite":
        return (SQLITE_FILENAME,)
    return tuple(f"{table.name}.{output_format}" for table in all_tables)
