"""Checkpointed grid search. Validation chooses one model; test evaluates it once."""
from __future__ import annotations

import importlib.metadata
import itertools
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.exceptions import ConvergenceWarning

from .config import Config
from .dataset import Catalog
from .io import file_hash, identity, read_json, write_json
from .model import fit_model, proxy_fee
from .ranking import evaluate
from .source import snapshot


@contextmanager
def output_lock(folder: Path):
    """OS-level lock is released even when a process is terminated."""
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / ".run.lock").open("a+b") as handle:
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError("Another process is using this output_dir") from error
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def atomic_model(model, path: Path):
    temporary = path.with_suffix(".tmp")
    joblib.dump(model, temporary, compress=3)
    os.replace(temporary, path)


def evaluate_model(catalog: Catalog, model, split: str, folder: Path, config: Config) -> dict:
    chunks = []
    fee_column = config.section("labels").get("fee_column")
    proxy = next(p for p in config.values["proxies"] if p["name"] == config.section("evaluation")["proxy"])
    for frame in catalog.batches(split):
        predictions = model.predict(frame)
        keep = ["_row_id", "_user", "_item", "_day", "_label"] + config.section("ranking").get("partition_columns", [])
        reduced = frame[list(dict.fromkeys(keep))].reset_index(drop=True)
        reduced = pd.concat([reduced, predictions.reset_index(drop=True)], axis=1)
        reduced["evaluation_proxy_fee"] = proxy_fee(frame, proxy)
        reduced["_observed_fee"] = frame[fee_column].to_numpy() if fee_column else float("nan")
        chunks.append(reduced)
    if not chunks:
        raise ValueError(f"The {split} split has no rows")
    # Feature-wide data is batched. Narrow prediction/evaluation tables must fit RAM.
    frame = pd.concat(chunks, ignore_index=True)
    fees = frame["_observed_fee"]
    if ((fees.notna()) & ((fees < 0) | ~np.isfinite(fees))).any():
        raise ValueError("Observed fees must be nonnegative and finite when supplied")
    if config.section("evaluation")["selection_metric"] == "macro_daily_observed_fee_yen" and ((frame["_label"] == 1) & fees.isna()).any():
        raise ValueError("Fee-based selection requires an observed fee on every positive evaluation row")
    return evaluate(frame, config.section("ranking"), folder)


def run(config: Config, *, retry_failed: bool = False) -> dict:
    with output_lock(config.output):
        if retry_failed and (config.output / "test.json").exists():
            raise ValueError("Cannot retry search after opening test results. Use a new independent holdout")
        source = snapshot(config)
        versions = {name: importlib.metadata.version(name) for name in
                    ("pandas", "numpy", "scikit-learn", "duckdb", "pyarrow", "joblib")}
        code = {path.name: file_hash(path) for path in sorted(Path(__file__).parent.glob("*.py"))}
        signature = identity({"config": config.values, "source": file_hash(source), "code": code,
                              "versions": versions, "python": sys.version})
        manifest = config.output / "experiment.json"
        if manifest.exists() and read_json(manifest)["signature"] != signature:
            raise ValueError("Code, dependencies, data or configuration changed. Use a new output_dir")
        write_json(manifest, {"signature": signature, "config": config.values, "code": code,
                              "versions": versions, "python": sys.version})
        results = []
        with Catalog(config, source) as catalog:
            write_json(config.output / "cohort.json", catalog.describe())
            write_json(config.output / "training_profile.json", catalog.profile())
            for plan, specification, scorer in itertools.product(config.values["datasets"], config.values["models"], config.values["scorers"]):
                name = "__".join((plan["name"], specification["name"], scorer["name"]))
                folder = config.output / "candidates" / name
                folder.mkdir(parents=True, exist_ok=True)
                checkpoint = folder / "validation.json"
                failure = folder / "failure.json"
                if checkpoint.exists():
                    saved = read_json(checkpoint)
                    if file_hash(folder / "model.joblib") != saved["model_sha256"]:
                        raise ValueError(f"Model checkpoint changed: {name}")
                    results.append(saved)
                    print(f"resume {name}", flush=True)
                    continue
                if failure.exists() and not retry_failed:
                    print(f"skip failed {name}; pass --retry-failed to retry", flush=True)
                    continue
                started = time.monotonic()
                try:
                    data_folder = config.output / "datasets" / plan["name"]
                    training_path = data_folder / "training.parquet"
                    if training_path.exists() and (data_folder / "sampling.json").exists():
                        if file_hash(training_path) != read_json(data_folder / "sampling.json")["file_sha256"]:
                            raise ValueError("Cached training data changed")
                        training = pd.read_parquet(training_path)
                    else:
                        training = catalog.sample(plan, data_folder)
                    proxy = next(p for p in config.values["proxies"] if p["name"] == scorer["proxy"])
                    fee_data = None
                    if scorer.get("fee_dataset"):
                        fee_plan = next(p for p in config.values["datasets"] if p["name"] == scorer["fee_dataset"])
                        fee_data = catalog.sample(fee_plan, config.output / "datasets" / fee_plan["name"])
                    model = fit_model(training, config.section("features"), specification, scorer, proxy,
                                      config.section("labels").get("fee_column"), config.section("experiment").get("seed", 42), fee_data)
                    model.metadata["experiment_signature"] = signature
                    atomic_model(model, folder / "model.joblib")
                    metrics = evaluate_model(catalog, model, "validation", folder / "validation", config)
                    saved = {"name": name, "metrics": metrics, "model": model.metadata,
                             "model_sha256": file_hash(folder / "model.joblib"),
                             "seconds": time.monotonic() - started}
                    write_json(checkpoint, saved)
                    failure.unlink(missing_ok=True)
                    results.append(saved)
                    print(f"completed {name}", flush=True)
                except (ValueError, RuntimeError, ConvergenceWarning) as error:
                    write_json(failure, {"name": name, "error_type": type(error).__name__, "error": str(error)})
                    print(f"failed {name}: {type(error).__name__}: {error}", flush=True)
            if not results:
                raise RuntimeError("All candidates failed; inspect candidates/*/failure.json")
            metric = config.section("evaluation")["selection_metric"]
            if any(result["metrics"].get(metric) is None for result in results):
                raise ValueError(f"Selection metric is unavailable: {metric}")
            reverse = config.section("evaluation").get("higher_is_better", True)
            ordered = sorted(results, key=lambda r: ((-1 if reverse else 1) * r["metrics"][metric], r["name"]))
            write_json(config.output / "leaderboard.json", ordered)
            best = ordered[0]
            test_checkpoint = config.output / "test.json"
            if test_checkpoint.exists():
                final = read_json(test_checkpoint)
                if final["name"] != best["name"]:
                    raise ValueError("Winner changed after test was opened. Start a fresh independent holdout")
            else:
                write_json(config.output / "selected.json", {"name": best["name"], "metric": metric,
                                                            "validation_value": best["metrics"][metric]})
                model = joblib.load(config.output / "candidates" / best["name"] / "model.joblib")
                final = {"name": best["name"], "metrics": evaluate_model(catalog, model, "test", config.output / "test", config)}
                write_json(test_checkpoint, final)
            return {"selected": best["name"], "candidates_completed": len(results), "test": final["metrics"]}
