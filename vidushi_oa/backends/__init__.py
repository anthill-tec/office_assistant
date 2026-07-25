"""CR-OA-018 §S1 — persistence backend factory.

`get_backend(name)` resolves the active persistence backend, selected by the `name` argument
or the `VIDUSHI_BACKEND` env var. `mongo` is the backend today; `sqlite` (the future default)
lands in §S2. An unknown name raises `ValueError`.
"""
import os

from vidushi_oa.backends.base import Backend
from vidushi_oa.backends.mongo import MongoBackend

_BACKENDS = {"mongo": MongoBackend}


def get_backend(name=None):
    """Return the persistence `Backend` for `name` (default from `VIDUSHI_BACKEND`, else mongo)."""
    name = name or os.environ.get("VIDUSHI_BACKEND", "mongo")
    try:
        return _BACKENDS[name]()
    except KeyError:
        raise ValueError(f"unknown backend {name!r}; known: {sorted(_BACKENDS)}")


__all__ = ["Backend", "get_backend"]
