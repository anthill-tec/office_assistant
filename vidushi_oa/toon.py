"""Public TOON encode/decode helper (CR-OA-011).

The implementation lives in `vidushi_oa._toon`; this module re-exports it so that both the
public `vidushi_oa.toon` import path and the `scripts/oa_toon.py` compat shim resolve to the
SAME callables (stable object identity across re-imports).
"""
from vidushi_oa._toon import *  # noqa: F401,F403
from vidushi_oa._toon import from_toon, to_toon  # noqa: F401
