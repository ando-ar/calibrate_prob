"""Allocate experiment runs, snapshot inputs, and locate published models."""

import hashlib
import json
import re
import shutil
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from itertools import count
from pathlib import Path
from tempfile import mkdtemp

JST = timezone(timedelta(hours=9))
METADATA = "run_metadata.json"
MODEL_FILE = "calibration_model.pkl"
RUN_ID = re.compile(r"([0-9]{8}_[0-9]{6})(?:_([0-9]{2,}))?")


def digest(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def write_json(path: Path, payload: Mapping):
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def write_status(run_dir: Path, metadata: Mapping):
    staging = run_dir / f".{METADATA}.tmp"
    write_json(staging, metadata)
    staging.replace(run_dir / METADATA)


def allocate_run(experiments_dir: Path, model_name: str, stage: str, started: datetime) -> Path:
    device_names = {"CON", "PRN", "AUX", "NUL", *(f"{kind}{n}" for kind in ("COM", "LPT") for n in range(10))}
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", model_name) or model_name.upper() in device_names:
        raise ValueError("model-name must be a portable directory name (1–80 ASCII letters, digits, _ or -)")
    directory = experiments_dir / model_name / stage
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = started.strftime("%Y%m%d_%H%M%S")
    for index in count():
        run_dir = directory / (timestamp if not index else f"{timestamp}_{index:02d}")
        try:
            run_dir.mkdir()
        except FileExistsError:
            continue
        return run_dir
    raise RuntimeError("Could not allocate a run")


@contextmanager
def experiment_run(experiments_dir: Path, model_name: str, stage: str, *, seed: int, sqlite: bool):
    started = datetime.now(JST)
    run_dir = allocate_run(experiments_dir, model_name, stage, started)
    hashes: dict[str, str] = {}
    metadata: dict = {
        "model_name": model_name, "stage": stage, "run_id": run_dir.name,
        "status": "running", "started_at": started.isoformat(), "sql_seed": seed,
        "warehouse": {"kind": "sqlite" if sqlite else "redshift"}, "input_sha256": hashes,
    }
    write_status(run_dir, metadata)
    try:
        inputs = run_dir / "input"
        inputs.mkdir()
        output = Path(mkdtemp(dir=run_dir, prefix=".calibration-"))
        published = run_dir / "output"
        try:
            yield run_dir, output, metadata, hashes
            for name, checksum in hashes.items():
                if digest(inputs / name) != checksum:
                    raise ValueError(f"Input changed during execution: {name}")
            output.rename(published)
            try:
                metadata.update(status="succeeded", finished_at=datetime.now(JST).isoformat())
                write_status(run_dir, metadata)
            except OSError:
                published.rename(output)
                raise
        finally:
            if output.exists() and not output.is_symlink() and output.resolve().parent == run_dir.resolve():
                shutil.rmtree(output)
    except BaseException as error:
        metadata.update(status="failed", finished_at=datetime.now(JST).isoformat(), error_type=type(error).__name__)
        write_status(run_dir, metadata)
        raise


def copy_inputs(inputs: Path, sources: Mapping[str, Path], hashes: dict[str, str]):
    for name, source in sources.items():
        destination = inputs / name
        staging = destination.with_suffix(".tmp")
        try:
            shutil.copyfile(source, staging)
            checksum = digest(staging)
            staging.rename(destination)
            hashes[name] = checksum
        finally:
            staging.unlink(missing_ok=True)


def eligible_training(train_dir: Path) -> Iterator[tuple[tuple[datetime, int], Path, dict]]:
    if not train_dir.is_dir():
        return
    for run in train_dir.iterdir():
        match = RUN_ID.fullmatch(run.name)
        if (not match or not run.is_dir() or run.is_symlink()
                or run.resolve().parent != train_dir.resolve() or not (run / "output" / MODEL_FILE).is_file()):
            continue
        try:
            metadata = json.loads((run / METADATA).read_text(encoding="utf-8"))
        except (FileNotFoundError, UnicodeError, json.JSONDecodeError):
            continue
        if (not isinstance(metadata, dict) or metadata.get("status") != "succeeded"
                or metadata.get("stage") != "train" or metadata.get("run_id") != run.name
                or metadata.get("model_name") != train_dir.parent.name
                or not isinstance(metadata.get("model_sha256"), str)
                or not isinstance(metadata.get("started_at"), str)):
            continue
        try:
            started = datetime.fromisoformat(metadata["started_at"])
        except ValueError:
            continue
        if started.utcoffset() == JST.utcoffset(None) and started.strftime("%Y%m%d_%H%M%S") == match[1]:
            yield (started, int(match[2] or 0)), run, metadata


def select_model(train_dir: Path, train_run: str | None) -> tuple[str, bytes, str]:
    if train_run is not None and not RUN_ID.fullmatch(train_run):
        raise ValueError("Invalid --train-run ID")
    models = ((order, path, metadata) for order, path, metadata in eligible_training(train_dir)
              if train_run is None or path.name == train_run)
    selection = max(models, key=lambda model: model[0], default=None)
    if selection is None:
        suffix = f" {train_run}" if train_run else ""
        raise ValueError(f"No successful training run{suffix} in {train_dir}")
    _, path, metadata = selection
    content = (path / "output" / MODEL_FILE).read_bytes()
    checksum = hashlib.sha256(content).hexdigest()
    if checksum != metadata["model_sha256"]:
        raise ValueError(f"Calibration model hash differs from training metadata for {path.name}")
    return path.name, content, checksum
