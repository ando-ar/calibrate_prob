"""External command execution for the demo GUI."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class CommandResult:
    """Outcome of one external command run."""

    command: list[str]
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str]], CommandResult]


def cli_command(argv: Sequence[str]) -> list[str]:
    """Build the command that runs the pipeline CLI with the given arguments."""
    return [sys.executable, "-m", "calibration_pipeline.cli", *argv]


def subprocess_runner(command: Sequence[str]) -> CommandResult:
    """Run one command synchronously and capture its output."""
    completed = subprocess.run(
        list(command), check=False, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return CommandResult(list(command), completed.returncode, completed.stdout, completed.stderr)
