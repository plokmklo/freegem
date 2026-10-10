"""Backwards-compatible module entry point: ``python -m gemini_bridge``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
