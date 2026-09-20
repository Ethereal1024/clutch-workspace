"""`python -m clutch_workspace` — the same entry the console script wraps."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
