"""Load the contract for one calibration run."""

import tomllib
from dataclasses import dataclass
from pathlib import Path

ROW_ID = "candidate_row_id"
ROLE = "row_role"
CANDIDATE_TABLE = "calibration_candidates"
USER_ID_BASE = 10_000_000
ITEM_ID_BASE = 20_000_000
ID_RANGE = 10_000_000


@dataclass(frozen=True)
class Config:
    score: str
    label: str
    probability: str
    numeric: tuple[str, ...]
    categorical: tuple[str, ...]
    bins: int

    @property
    def features(self) -> tuple[str, ...]:
        return (*self.numeric, *self.categorical)


def read_config(path: Path) -> dict:
    with path.open("rb") as file:
        return tomllib.load(file)


def load_config(path: Path) -> Config:
    model = read_config(path)["model"]
    numeric = model.get("numeric_features", [])
    categorical = model.get("categorical_features", [])
    if not isinstance(numeric, list) or not isinstance(categorical, list):
        raise TypeError("Feature settings must be lists of column names")
    names = [model["score"], model.get("label", "label"), model.get("probability", "calibrated_probability"),
             *numeric, *categorical]
    if any(not isinstance(name, str) or not name.strip() for name in names):
        raise ValueError("Column names must be nonempty strings")
    if len(set(names)) != len(names) or {ROW_ID, ROLE}.intersection(names):
        raise ValueError("Column names must be distinct and cannot use reserved names")
    bins = model.get("bins", 10)
    if type(bins) is not int or not 2 <= bins <= 100:
        raise ValueError("bins must be an integer from 2 to 100")
    return Config(names[0], names[1], names[2], tuple(numeric), tuple(categorical), bins)
