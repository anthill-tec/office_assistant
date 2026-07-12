"""MongoDB connection helper for the office-assistant store (CR-OA-001).

Pinned to the local *office_assistant* instance on 127.0.0.1:27017 — port 27018 hosts the
user's platform DBs and is off-limits. Configuration via environment (read lazily so a caller
or test can override per-call):
  VIDUSHI_MONGO_URI   default mongodb://127.0.0.1:27017
  VIDUSHI_MONGO_DB    default vidushi_oa
No secrets in code (local bind, no auth).
"""
import os

from pymongo import MongoClient

_DEFAULT_URI = "mongodb://127.0.0.1:27017"
_DEFAULT_DB = "vidushi_oa"
_client = None


def _uri():
    return os.environ.get("VIDUSHI_MONGO_URI", _DEFAULT_URI)


def _db_name():
    return os.environ.get("VIDUSHI_MONGO_DB", _DEFAULT_DB)


def client():
    """Process-wide cached MongoClient for the configured URI."""
    global _client
    if _client is None:
        _client = MongoClient(_uri(), serverSelectionTimeoutMS=2000)
    return _client


def db():
    """The office-assistant database (name honours VIDUSHI_MONGO_DB at call time)."""
    return client()[_db_name()]


def coll(t):
    """The collection for store type `t` (collection name == type name)."""
    return db()[t]
