"""Public store-access helper for the vidushi office-assistant store (CR-OA-011).

`client`/`db` re-export the MongoDB helpers in `vidushi_oa._mongo`; `coll(t)` now routes through
the pluggable backend seam (`vidushi_oa.backends`, CR-OA-018) so the CLI's collection access is
backend-agnostic. Both the public `vidushi_oa.mongo` path and the `scripts/oa_mongo.py` shim
resolve to the same callables.
"""
from vidushi_oa._mongo import client, db  # noqa: F401
from vidushi_oa.backends import get_backend


def coll(t):
    """Collection handle for store type `t`, via the active persistence backend (CR-OA-018)."""
    return get_backend().collection(t)
