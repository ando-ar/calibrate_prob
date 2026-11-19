"""Generate several independent market seeds concurrently (issue #40).

One fresh ``python -m synthetic_funnel`` subprocess is used per seed.  The child
runs the exact same code path as a solo generation, with only the seed and the
destination directory changed, so every file it writes is byte-identical to a
solo run of that seed.  No RNG state, warmup model or import cache is shared
between seeds.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SEED_DIRECTORY = "seed-{seed}"


def seed_output_dir(root: str | Path, seed: int) -> Path:
    """Return the dedicated run directory for one seed."""
    return Path(root) / SEED_DIRECTORY.format(seed=seed)


def _child_command(config: Path, output: Path, seed: int, output_format: str,
                   seeker_arrivals: int | None, job_arrivals: int | None,
                   progress: bool) -> list[str]:
    command = [sys.executable, "-m", "synthetic_funnel",
               "--config", str(config), "--output", str(output),
               "--format", output_format, "--seed", str(seed)]
    if seeker_arrivals is not None:
        command.extend(("--seeker-arrivals", str(seeker_arrivals)))
    if job_arrivals is not None:
        command.extend(("--job-arrivals", str(job_arrivals)))
    if progress:
        command.append("--progress")
    return command


def _child_environment() -> Mapping[str, str]:
    """Let each child import the same source tree as the calling process."""
    environment = os.environ.copy()
    package_parent = Path(__file__).resolve().parents[1]  # the ``src`` directory
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(package_parent), environment.get("PYTHONPATH", "")) if part
    )
    return environment


def _run_child(command: list[str], environment: Mapping[str, str], seed: int) -> dict:
    completed = subprocess.run(command, capture_output=True, text=True,
                               env=environment, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"seed {seed} failed (exit {completed.returncode}): "
                           f"{completed.stderr.strip()}")
    if completed.stderr:
        print(completed.stderr, end="" if completed.stderr.endswith("\n") else "\n",
              file=sys.stderr)
    summary = json.loads(completed.stdout)
    return {"output": summary["output"], "rows": summary["rows"]}


def generate_seeds(
    config: str | Path,
    output: str | Path,
    seeds: Sequence[int],
    *,
    jobs: int | None = None,
    output_format: str = "sqlite",
    seeker_arrivals: int | None = None,
    job_arrivals: int | None = None,
    progress: bool = False,
) -> dict[int, dict]:
    """Generate one independent dataset per seed, bounded by ``jobs`` processes.

    Returns a mapping from each seed to its ``{"output": path, "rows": count}``
    summary.  The default concurrency is the smaller of the seed count and the
    CPU count; ``jobs`` raises or lowers that bound.  A failing child aborts the
    batch with a ``RuntimeError`` that includes the child's stderr.
    """
    seeds = tuple(seeds)
    if not seeds:
        raise ValueError("seeds must not be empty")
    if any(not isinstance(seed, int) or isinstance(seed, bool) for seed in seeds):
        raise TypeError("seeds must be integers")
    if len(set(seeds)) != len(seeds):
        raise ValueError("seeds must be unique")
    if jobs is not None and jobs < 1:
        raise ValueError("jobs must be >= 1")
    config = Path(config).resolve()
    if not config.is_file():
        raise FileNotFoundError(config)
    root = Path(output)
    root.mkdir(parents=True, exist_ok=True)
    workers = min(len(seeds), jobs if jobs is not None else (os.cpu_count() or 1))
    environment = _child_environment()
    results: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for seed in seeds:
            command = _child_command(config, seed_output_dir(root, seed), seed, output_format,
                                     seeker_arrivals, job_arrivals, progress)
            futures[pool.submit(_run_child, command, environment, seed)] = seed
        for future in as_completed(futures):
            results[futures[future]] = future.result()
    return {seed: results[seed] for seed in seeds}
