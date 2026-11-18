"""Validate user data and map the SQL result back to the original candidate rows."""

import csv
from pathlib import Path

import numpy as np
import polars as pl

from calibration_pipeline.config import (
    ID_RANGE,
    ITEM_ID_BASE,
    ROLE,
    ROW_ID,
    USER_ID_BASE,
    Config,
)


def require(frame: pl.DataFrame, columns, context: str):
    if missing := set(columns) - set(frame.columns):
        raise ValueError(f"{context}: missing columns: {', '.join(sorted(missing))}")


def numbers(values: pl.Series, context: str) -> pl.Series:
    try:
        converted = values.cast(pl.Float64)
    except (pl.exceptions.PolarsError, TypeError) as error:
        raise ValueError(f"{context}: expected numeric values") from error
    if not converted.is_finite().fill_null(False).all():
        raise ValueError(f"{context}: missing or non-finite numeric values")
    return converted


def read_candidates(path: Path) -> pl.DataFrame:
    if path.suffix == ".parquet":
        return pl.read_parquet(path)
    with path.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.reader(file, strict=True)
        try:
            columns = next(reader, [])
            if not columns or len(columns) != len(set(columns)) or "" in columns:
                raise ValueError("Candidate CSV requires nonempty and unique header names")
            for row in reader:
                if row and len(row) != len(columns):
                    raise ValueError(f"Candidate CSV line {reader.line_num}: wrong number of fields")
        except csv.Error as error:
            raise ValueError(f"Candidate CSV line {reader.line_num}: {error}") from error
    return pl.read_csv(path, infer_schema=False)


def integer_candidate_ids(frame: pl.DataFrame, *, csv_input: bool) -> pl.DataFrame:
    require(frame, ("user_id", "item_id"), "original_score")
    ids = []
    for name, first in (("user_id", USER_ID_BASE), ("item_id", ITEM_ID_BASE)):
        values = frame[name]
        first_digit = str(first)[0]
        message = f"original_score: {name} must be an 8-digit integer starting with {first_digit}"
        if csv_input:
            if values.dtype != pl.String or not values.str.contains(rf"\A{first_digit}[0-9]{{7}}\z").fill_null(False).all():
                raise ValueError(message)
        elif not values.dtype.is_integer():
            raise ValueError(message)
        if not values.cast(pl.Int64, strict=False).is_between(first, first + ID_RANGE - 1).fill_null(False).all():
            raise ValueError(message)
        ids.append(values.cast(pl.Int64))
    return frame.with_columns(ids)


def upload_frame(original: pl.DataFrame, config: Config) -> pl.DataFrame:
    require(original, [config.score], "original_score")
    if original.is_empty():
        raise ValueError("original_score contains no candidates")
    if reserved := {name.lower() for name in original.columns} & {ROW_ID, config.probability.lower()}:
        raise ValueError(f"original_score already contains reserved columns: {', '.join(sorted(reserved))}")
    if len({name.lower() for name in original.columns}) != original.width:
        raise ValueError("Candidate column names must be unique ignoring case")
    return original.with_columns(
        pl.int_range(pl.len(), dtype=pl.Int64).alias(ROW_ID), numbers(original[config.score], config.score),
    )


def rows_by_role(rows: pl.DataFrame, roles: tuple[str, ...]) -> dict[str, pl.DataFrame]:
    if not rows[ROLE].is_in(roles).fill_null(False).all():
        raise ValueError(f"row_role must be one of {', '.join(roles)}")
    frames = {role: rows.filter(pl.col(ROLE) == role) for role in roles}
    for role, frame in frames.items():
        if frame.is_empty():
            raise ValueError(f"{role}: no rows returned")
    return frames


def training_rows(rows: pl.DataFrame, config: Config) -> dict[str, pl.DataFrame]:
    require(rows, [ROLE, config.score, config.label, *config.features], "train.sql")
    return rows_by_role(rows, ("train", "calibrate", "validation"))


def prediction_rows(predicted: pl.DataFrame, uploaded: pl.DataFrame, config: Config) -> pl.DataFrame:
    require(predicted, [ROW_ID, config.score, *config.features], "inference SQL")
    ids = predicted[ROW_ID]
    if not ids.dtype.is_integer() or ids.null_count() or len(ids) != len(uploaded) or ids.n_unique() != len(uploaded):
        raise ValueError("predict: candidate_row_id must identify each uploaded row exactly once")
    ordered = predicted.sort(ROW_ID)
    if not ordered[ROW_ID].cast(pl.Int64, strict=True).equals(uploaded[ROW_ID]):
        raise ValueError("predict: unknown or missing candidate_row_id")
    if not sql_numbers(ordered[config.score], "predict score").equals(uploaded[config.score]):
        raise ValueError("predict: returned score differs from the uploaded score for a candidate_row_id")
    return ordered


def sql_numbers(values: pl.Series, context: str, *, nullable: bool = False) -> pl.Series:
    if nullable and values.dtype == pl.Null:
        return values.cast(pl.Float64)
    if not values.dtype.is_numeric():
        raise ValueError(f"{context}: expected a numeric column")
    if not nullable:
        return numbers(values, context)
    converted = values.cast(pl.Float64)
    if not (converted.is_finite() | converted.is_nan()).fill_null(True).all():
        raise ValueError(f"{context}: infinite numeric values")
    return converted


def categorical_text(values: pl.Series, name: str) -> pl.Series:
    """Map one categorical column to its training-time string form.

    The user decides per column whether a code such as 1, 2, 3 is an order
    (numeric feature) or a category (this list). Numeric values become stable
    strings here, so an integer in the training SQL and its string spelling in
    a candidate CSV land in the same category.
    """
    if values.dtype not in (pl.String, pl.Null):
        if not values.dtype.is_numeric():
            raise ValueError(f"{name}: expected a categorical string or numeric column")
        numeric = sql_numbers(values, name, nullable=True)
        if numeric.dtype.is_integer():
            values = numeric.cast(pl.String)
        else:
            # An integral float such as 1.0 and its integer spelling 1 name one category.
            converted = numeric.cast(pl.Float64)
            whole = converted.cast(pl.Int64, strict=False)
            integral = (converted == converted.round(0)) & whole.is_not_null()
            values = pl.select(
                pl.when(pl.Series(integral)).then(pl.Series(whole)).otherwise(pl.Series(converted))
                .cast(pl.String).alias(name)
            ).to_series()
    # A tag on every present value keeps all possible strings distinct from null.
    frame = pl.DataFrame([values.alias(name)])
    return frame.select(
        (pl.lit("1") + pl.col(name).cast(pl.String)).fill_null("0").alias(name)
    ).to_series()


def labels(frame: pl.DataFrame, config: Config, role: str) -> np.ndarray:
    y = sql_numbers(frame[config.label], f"{role}.{config.label}")
    if not y.is_in([0, 1]).all():
        raise ValueError(f"{role}: labels must be binary (0 or 1)")
    if role != "validation" and y.n_unique() != 2:
        raise ValueError(f"{role}: both classes required (n={len(y)}, positives={int(y.sum() or 0)})")
    return y.to_numpy()


def model_frame(frame: pl.DataFrame, config: Config) -> pl.DataFrame:
    columns = [sql_numbers(frame[config.score], config.score)]
    columns.extend(sql_numbers(frame[name], name, nullable=True) for name in config.numeric)
    columns.extend(categorical_text(frame[name], name) for name in config.categorical)
    return pl.DataFrame(columns)
