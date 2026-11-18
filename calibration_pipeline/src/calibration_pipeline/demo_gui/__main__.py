"""Launch the calibration pipeline demo GUI."""

from __future__ import annotations

import sys

from calibration_pipeline.demo_gui.server import resolve_base, serve


def main() -> int:
    """Start the demo server on the resolved project base directory."""
    serve(resolve_base())
    return 0


if __name__ == "__main__":
    sys.exit(main())
