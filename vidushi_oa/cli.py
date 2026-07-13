"""Public CLI entry point for the vidushi office-assistant store (CR-OA-011).

The implementation lives in `vidushi_oa._cli`; this module re-exports it so that the console
script (`voa = vidushi_oa.cli:main`), the public `vidushi_oa.cli` import path, and the
`scripts/store.py` compat shim all resolve to the SAME `main` object (stable object identity
across re-imports).
"""
from vidushi_oa._cli import *  # noqa: F401,F403
from vidushi_oa._cli import main  # noqa: F401

if __name__ == "__main__":
    main()
