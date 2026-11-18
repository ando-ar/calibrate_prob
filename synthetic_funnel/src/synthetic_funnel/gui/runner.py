"""External command execution for the synthetic funnel GUI."""

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
    """Build the command that runs the funnel CLI with the given arguments."""
    return [sys.executable, "-m", "synthetic_funnel", *argv]


def subprocess_runner(command: Sequence[str]) -> CommandResult:
    """Run one command synchronously and capture its output."""
    completed = subprocess.run(
        list(command), check=False, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return CommandResult(list(command), completed.returncode, completed.stdout, completed.stderr)
