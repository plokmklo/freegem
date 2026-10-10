"""Backwards-compatible module entry point: ``python -m freegem``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
