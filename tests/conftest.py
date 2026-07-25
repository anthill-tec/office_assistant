"""CR-OA-018 §S3 — pin the Mongo default for the existing suite.

The production default backend flipped to SQLite (§S3). The pre-existing Mongo CLI tests copy
`os.environ` into their subprocess env and expect the mongo backend, so this autouse fixture
pins `VIDUSHI_BACKEND=mongo` for every test. Tests that exercise sqlite/migration/default
behaviour override it in their own `setUp` (setting or popping `VIDUSHI_BACKEND`).
"""
import os

import pytest


@pytest.fixture(autouse=True)
def _default_backend_mongo():
    saved = os.environ.get("VIDUSHI_BACKEND")
    os.environ["VIDUSHI_BACKEND"] = "mongo"
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("VIDUSHI_BACKEND", None)
        else:
            os.environ["VIDUSHI_BACKEND"] = saved
