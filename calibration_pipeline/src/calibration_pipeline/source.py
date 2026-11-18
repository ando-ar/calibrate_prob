"""Upload the candidate file and execute the user's SQL without substitutions."""

import os
import random
import sqlite3
from collections.abc import Generator
from contextlib import closing, contextmanager
from pathlib import Path

import polars as pl
from sqlalchemy import (
    URL,
    BigInteger,
    Boolean,
    Column,
    Connection,
    Date,
    DateTime,
    Double,
    LargeBinary,
    MetaData,
    Numeric,
    String,
    Table,
    create_engine,
)

from calibration_pipeline.config import CANDIDATE_TABLE

DEFAULT_SQL_SEED = 42
MAX_SQL_SEED = 2**31 - 1


def candidate_table(frame: pl.DataFrame) -> Table:
    columns = []
    for name, dtype in frame.schema.items():
        if dtype.is_integer():
            column_type = BigInteger()
        elif dtype.is_float():
            column_type = Double()
        elif isinstance(dtype, pl.Decimal):
            column_type = Numeric(dtype.precision or 38, dtype.scale)
        elif dtype == pl.Boolean:
            column_type = Boolean()
        elif dtype == pl.Date:
            column_type = Date()
        elif isinstance(dtype, pl.Datetime):
            column_type = DateTime(timezone=dtype.time_zone is not None)
        elif dtype == pl.Binary:
            column_type = LargeBinary()
        elif dtype in (pl.String, pl.Categorical, pl.Enum, pl.Null):
            column_type = String(65535)
        else:
            raise ValueError(f"original_score: {name} has an unsupported upload type: {dtype}")
        columns.append(Column(name, column_type))
    return Table(CANDIDATE_TABLE, MetaData(), *columns, prefixes=["TEMPORARY"])


class Source:
    """同じSQLAlchemy接続で候補のアップロードとSQL実行を行う。"""

    def __init__(self, connection: Connection):
        self.connection = connection

    def set_seed(self, seed: int) -> None:
        """接続のSQL乱数を0〜2147483647の整数seedで初期化する。"""
        if type(seed) is not int or not 0 <= seed <= MAX_SQL_SEED:
            raise ValueError(f"SQL seed must be an integer between 0 and {MAX_SQL_SEED}")
        if self.connection.dialect.name == "sqlite":
            target = self.connection.connection.driver_connection
            if not isinstance(target, sqlite3.Connection):
                raise TypeError("Seeding SQLite requires a sqlite3 connection")
            rng = random.Random(seed)
            limit = 2**63 - 1
            target.create_function("random", 0, lambda: rng.randint(-limit, limit))
            return

        # Redshift accepts a numeric literal from 0 to 1 and scales it to a 31-bit seed.
        with self.connection.exec_driver_sql(f"SET SEED TO {seed / MAX_SQL_SEED:.17f}"):
            pass

    def upload(self, frame: pl.DataFrame):
        """候補を接続内の一時テーブルcalibration_candidatesに格納する。"""
        table = candidate_table(frame)
        table.create(self.connection)
        insert = table.insert()
        parameter_limit = 900 if self.connection.dialect.name == "sqlite" else 30_000
        batch_size = max(1, min(500, parameter_limit // frame.width))
        for batch in frame.iter_slices(n_rows=batch_size):
            self.connection.execute(insert.values(batch.to_dicts()))

    def execute_sql(self, sql: str, params: tuple = ()) -> pl.DataFrame:
        """SQLを実行し、結果をPolars DataFrameとして返す。

        ``params`` はDBドライバー形式の位置引数としてバインドする。
        結果表を返さないSQLではValueErrorを送出する。
        """
        with self.connection.exec_driver_sql(
            sql, params or None, execution_options={"no_parameters": not params},
        ) as result:
            if not result.returns_rows:
                raise ValueError("SQL must return a result set")
            return pl.DataFrame(result.fetchall(), schema=list(result.keys()),
                                orient="row", infer_schema_length=None)

    def execute_sql_file(self, path: Path) -> pl.DataFrame:
        """UTF-8（BOM可）のSQLファイルを読み込み、置換せず実行する。"""
        return self.execute_sql(path.read_text(encoding="utf-8-sig"))


@contextmanager
def connect(*, sqlite: bool = False, sqlite_fixture: Path | None = None) -> Generator[Source]:
    """トランザクション内のSourceを提供するコンテキストマネージャー。

    SQLiteはメモリ内DBを作り、指定されたfixtureをコピーする。
    既定のRedshiftはREDSHIFT_HOST/DATABASE/USER/PASSWORDと任意のPORTを使う。
    コンテキストを抜けると接続とengineを解放する。
    """
    if sqlite:
        url = URL.create("sqlite")
        options = {}
    else:
        keys = ("HOST", "DATABASE", "USER", "PASSWORD")
        values = {key: os.environ.get(f"REDSHIFT_{key}") for key in keys}
        if missing := [f"REDSHIFT_{key}" for key, value in values.items() if not value]:
            raise ValueError(f"Missing environment variables: {', '.join(missing)}")
        url = URL.create("redshift+redshift_connector", host=values["HOST"], database=values["DATABASE"],
                         username=values["USER"], password=values["PASSWORD"],
                         port=int(os.environ.get("REDSHIFT_PORT", "5439")))
        options = {"ssl": True, "sslmode": "verify-full", "timeout": 60}
    engine = create_engine(url, connect_args=options, hide_parameters=True)
    try:
        with engine.connect() as connection:
            if sqlite and sqlite_fixture is not None:
                target = connection.connection.driver_connection
                if not isinstance(target, sqlite3.Connection):
                    raise TypeError("The SQLite fixture requires a sqlite3 connection")
                try:
                    with closing(sqlite3.connect(f"{sqlite_fixture.resolve().as_uri()}?mode=ro", uri=True)) as fixture:
                        fixture.backup(target)
                except sqlite3.Error as error:
                    raise ValueError(f"{sqlite_fixture.name}: {error}") from error
            with connection.begin():
                yield Source(connection)
    finally:
        engine.dispose()
