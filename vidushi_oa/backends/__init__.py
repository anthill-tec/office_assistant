"""CR-OA-018 §S3 — persistence backend factory.

`get_backend(name)` resolves the active persistence backend, selected by the `name` argument
or the `VIDUSHI_BACKEND` env var. `sqlite` is the default backend; `mongo` remains selectable.
Backend modules are imported LAZILY so a SQLite-only install (no pymongo) works — pymongo is
imported ONLY when the mongo backend is requested. An unknown name raises `ValueError`.
"""
import os

from vidushi_oa.backends.base import Backend


def get_backend(name=None):
    """Return the persistence `Backend` for `name` (default from `VIDUSHI_BACKEND`, else sqlite)."""
    name = name or os.environ.get("VIDUSHI_BACKEND", "sqlite")
    if name == "sqlite":
        from vidushi_oa.backends.sqlite import SqliteBackend

        return SqliteBackend()
    if name == "mongo":
        from vidushi_oa.backends.mongo import MongoBackend

        return MongoBackend()
    raise ValueError(f"unknown backend {name!r}; known: ['mongo', 'sqlite']")


__all__ = ["Backend", "get_backend"]
