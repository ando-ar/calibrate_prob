"""Build a clean, offline-readable HTML snapshot for Git."""

import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


def main() -> None:
    """Replace docs/html only after a warning-free Sphinx build succeeds."""
    root = Path(__file__).resolve().parents[1]
    docs = root / "docs"
    generated = docs / "_generated"
    destination = docs / "html"
    for path in (generated, destination):
        if path.is_symlink() or path.resolve().parent != docs.resolve():
            raise ValueError(f"Expected a directory directly under {docs}: {path}")
    if generated.exists():
        shutil.rmtree(generated)
    with TemporaryDirectory(prefix="calibration-docs-") as temporary:
        output = Path(temporary) / "html"
        subprocess.run(
            [sys.executable, "-m", "sphinx", "-E", "-a", "-W", "--keep-going",
             "-b", "html", "-d", str(Path(temporary) / "doctrees"), str(docs), str(output)],
            cwd=root, check=True,
        )
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(output, destination)
    print(f"Open {destination / 'index.html'}")


if __name__ == "__main__":
    main()
