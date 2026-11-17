"""Fit, evaluate and apply one experiment's CatBoost and isotonic calibrator."""

import pickle
from dataclasses import dataclass
from pathlib import Path

import catboost
import numpy as np
import polars as pl
import sklearn
from catboost import CatBoostClassifier
from sklearn.isotonic import IsotonicRegression

from calibration_pipeline import contracts
from calibration_pipeline.config import (
    CANDIDATE_TABLE,
    Config,
    load_config,
    read_config,
)
from calibration_pipeline.evaluation import metrics, write_plot
from calibration_pipeline.experiments import (
    MODEL_FILE,
    copy_inputs,
    digest,
    experiment_run,
    select_model,
    write_json,
)
from calibration_pipeline.source import DEFAULT_SQL_SEED, connect


@dataclass
class Calibrator:
    config: Config
    isotonic: IsotonicRegression
    base: CatBoostClassifier

    def input_scores(self, frame: pl.DataFrame) -> np.ndarray:
        features = contracts.model_frame(frame, self.config)
        return self.base.predict_proba(features)[:, 1]

    def predict(self, frame: pl.DataFrame) -> np.ndarray:
        return self.isotonic.predict(self.input_scores(frame))


def fit(config: Config, frames: dict[str, pl.DataFrame], seed: int) -> Calibrator:
    base = CatBoostClassifier(
        iterations=200, depth=4, random_seed=seed, allow_writing_files=False, verbose=False, thread_count=1,
    )
    model = Calibrator(config, IsotonicRegression(out_of_bounds="clip"), base)
    training = frames["train"]
    base.fit(contracts.model_frame(training, config), contracts.labels(training, config, "train"),
             cat_features=list(config.categorical))
    calibration = frames["calibrate"]
    model.isotonic.fit(model.input_scores(calibration), contracts.labels(calibration, config, "calibrate"))
    return model


def snapshot_inputs(
    run_dir: Path, config_path: Path, sql_path: Path, hashes: dict[str, str],
    *, sqlite: bool, sqlite_fixture: Path | None, original_score: Path | None = None,
):
    sql_name = "train.sql" if original_score is None else "inference.sql"
    files = {"config.toml": config_path, sql_name: sql_path}
    if original_score is not None:
        extension = original_score.suffix.lower()
        if extension not in {".csv", ".parquet"}:
            raise ValueError("--original-score must be CSV or Parquet")
        files[f"original_score{extension}"] = original_score
    if sqlite_fixture is not None:
        if not sqlite:
            raise ValueError("--sqlite-fixture requires --sqlite")
        files["sqlite_fixture.db"] = sqlite_fixture
    inputs = run_dir / "input"
    copy_inputs(inputs, files, hashes)
    fixture = inputs / "sqlite_fixture.db" if sqlite_fixture is not None else None
    return inputs, fixture


def save_training(output: Path, model: Calibrator, frames: dict[str, pl.DataFrame], metadata: dict):
    config = model.config
    validation = frames["validation"]
    y = contracts.labels(validation, config, "validation")
    raw_scores = contracts.sql_numbers(validation[config.score], config.score).to_numpy()
    raw = metrics(y, raw_scores, config.bins) if np.all((0 <= raw_scores) & (raw_scores <= 1)) else None
    before_scores = model.input_scores(validation)
    before = metrics(y, before_scores, config.bins)
    after = metrics(y, model.isotonic.predict(before_scores), config.bins)
    model_path = output / MODEL_FILE
    with model_path.open("wb") as file:
        pickle.dump(model, file, protocol=pickle.HIGHEST_PROTOCOL)
    metadata["model_sha256"] = digest(model_path)
    report = {
        **metadata,
        "status": "succeeded",
        "method": "catboost_isotonic",
        "catboost_version": catboost.__version__,
        "sklearn_version": sklearn.__version__,
        "rows": {role: len(frame) for role, frame in frames.items()},
        "validation_positives": int(y.sum()), "validation_positive_rate": float(y.mean()),
        "binning": {"scheme": "equal_width", "count": config.bins},
        "original_score_as_probability": raw,
        "before_input": "catboost_probability",
        "before_isotonic": before, "after_isotonic": after,
    }
    write_json(output / "calibration_metrics.json", report)
    write_plot(output / "calibration_plot.svg", after, before, "CatBoost before isotonic", len(y))


def save_predictions(output: Path, score_path: Path, original: pl.DataFrame, predicted: pl.DataFrame, model: Calibrator):
    predictions = original.with_columns(pl.Series(model.config.probability, model.predict(predicted)))
    if score_path.suffix == ".parquet":
        predictions.write_parquet(output / "calibrated_score.parquet")
    else:
        predictions.write_csv(output / "calibrated_score.csv")


def train(
    experiments_dir: Path, model_name: str, config_path: Path, sql_path: Path,
    *, sqlite: bool = False, seed: int = DEFAULT_SQL_SEED, sqlite_fixture: Path | None = None,
) -> Path:
    with experiment_run(experiments_dir, model_name, "train", seed=seed, sqlite=sqlite) as run:
        run_dir, output, metadata, hashes = run
        inputs, fixture = snapshot_inputs(run_dir, config_path, sql_path, hashes, sqlite=sqlite,
                                          sqlite_fixture=sqlite_fixture)
        config = load_config(inputs / "config.toml")
        metadata["score_column"] = config.score
        with connect(sqlite=sqlite, sqlite_fixture=fixture) as source:
            source.set_seed(seed)
            frames = contracts.training_rows(source.execute_sql_file(inputs / "train.sql"), config)
        save_training(output, fit(config, frames, seed), frames, metadata)
    return run_dir


def inference(
    experiments_dir: Path, model_name: str, config_path: Path, sql_path: Path, original_score: Path,
    *, train_run: str | None = None, sqlite: bool = False, seed: int = DEFAULT_SQL_SEED,
    sqlite_fixture: Path | None = None,
) -> Path:
    with experiment_run(experiments_dir, model_name, "inference", seed=seed, sqlite=sqlite) as run:
        run_dir, output, metadata, hashes = run
        # Resolve before copying large inputs so another training job cannot change the selection.
        try:
            selection = select_model(run_dir.parent.parent / "train", train_run)
        except (OSError, ValueError):
            snapshot_inputs(run_dir, config_path, sql_path, hashes, sqlite=sqlite,
                            sqlite_fixture=sqlite_fixture, original_score=original_score)
            raise
        selected_run, model_bytes, checksum = selection
        metadata.update(train_run_id=selected_run, model_sha256=checksum)
        inputs, fixture = snapshot_inputs(run_dir, config_path, sql_path, hashes, sqlite=sqlite,
                                          sqlite_fixture=sqlite_fixture, original_score=original_score)
        if read_config(inputs / "config.toml"):
            raise ValueError("Inference config is reserved and must contain no settings; column settings come from the model")
        try:
            model = pickle.loads(model_bytes)
        except (pickle.UnpicklingError, EOFError, AttributeError) as error:
            raise ValueError(f"Could not read calibration model for {selected_run}") from error
        if not isinstance(model, Calibrator) or not isinstance(model.base, CatBoostClassifier):
            raise TypeError(f"Expected a CatBoost calibrator for {selected_run}")

        score_path = inputs / f"original_score{original_score.suffix.lower()}"
        original = contracts.integer_candidate_ids(
            contracts.read_candidates(score_path), csv_input=score_path.suffix == ".csv",
        )
        uploaded = contracts.upload_frame(original, model.config)
        metadata.update(score_column=model.config.score,
                        warehouse={"kind": "sqlite" if sqlite else "redshift", "table": CANDIDATE_TABLE})
        with connect(sqlite=sqlite, sqlite_fixture=fixture) as source:
            source.set_seed(seed)
            source.upload(uploaded)
            rows = contracts.prediction_rows(source.execute_sql_file(inputs / "inference.sql"), uploaded, model.config)
        save_predictions(output, score_path, original, rows, model)
        write_json(output / "inference_metadata.json", {
            **metadata, "status": "succeeded", "catboost_version": catboost.__version__,
            "sklearn_version": sklearn.__version__, "rows": len(rows),
        })
    return run_dir
