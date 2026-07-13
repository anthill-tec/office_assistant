"""Compat shim (CR-OA-011) — re-exports the TOON encoder from `vidushi_oa.toon`.

`vidushi_oa.toon` in turn re-exports `vidushi_oa._toon`, so `to_toon`/`from_toon` here are
the SAME callables as `vidushi_oa.toon.to_toon`/`from_toon` (identity holds). A star re-export
(rather than a `sys.modules` alias) is used because the CR-OA-009 suite loads this file by
path via `importlib.util.exec_module` and inspects the resulting module's own namespace.
"""
from vidushi_oa.toon import *  # noqa: F401,F403
from vidushi_oa.toon import from_toon, to_toon  # noqa: F401
