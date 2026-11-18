"""Launch the synthetic funnel generation GUI."""

import sys

from synthetic_funnel.gui.server import resolve_base, serve


def main() -> int:
    """Start the generation GUI server on the resolved project base directory."""
    serve(resolve_base())
    return 0


if __name__ == "__main__":
    sys.exit(main())
