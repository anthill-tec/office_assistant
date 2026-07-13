"""Public MongoDB helper for the vidushi office-assistant store (CR-OA-011).

The implementation lives in `vidushi_oa._mongo`; this module re-exports it so that both
the public `vidushi_oa.mongo` import path and the `scripts/oa_mongo.py` compat shim resolve
to the SAME callables (stable object identity across re-imports).
"""
from vidushi_oa._mongo import *  # noqa: F401,F403
from vidushi_oa._mongo import client, coll, db  # noqa: F401
