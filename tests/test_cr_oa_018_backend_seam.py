"""CR-OA-018 §S1 — pluggable persistence backend seam.

RED: the `vidushi_oa.backends` factory + `Backend`/`Collection` interface don't exist yet, so
`get_backend` import fails. The seam must resolve a backend by name (or `VIDUSHI_BACKEND`),
expose a `collection(type_)` returning a store object with the collection operations the CLI
uses, and reject an unknown backend. `mongo` is the concrete backend in this slice (SQLite lands
in §S2); the interface is what §S2's SQLite backend implements.

Pure interface test — `collection(type_)` builds the store handle lazily and needs no live
mongod (a pymongo Collection is created without connecting).
"""
import os
import unittest

# The collection operations `_cli.py` invokes on `oa_mongo.coll(...)` (surveyed 2026-07-26).
REQUIRED_COLLECTION_OPS = (
    "find", "find_one", "insert_one", "replace_one", "update_one", "update_many",
    "delete_one", "count_documents", "aggregate", "create_index",
)


class BackendSeamTest(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("VIDUSHI_BACKEND", None)

    def test_mongo_backend_resolves_and_exposes_collection_ops(self):
        from vidushi_oa.backends import get_backend

        be = get_backend("mongo")
        self.assertEqual(be.name, "mongo")
        self.assertTrue(hasattr(be, "collection"))
        coll = be.collection("subscriptions")
        for op in REQUIRED_COLLECTION_OPS:
            self.assertTrue(hasattr(coll, op), f"backend collection missing op: {op}")

    def test_backend_exposes_db_level_helpers(self):
        from vidushi_oa.backends import get_backend

        be = get_backend("mongo")
        # db-level surface the CLI uses: collection provisioning + the db name
        for attr in ("db_name", "list_collections", "provision"):
            self.assertTrue(hasattr(be, attr), f"backend missing {attr}")

    def test_unknown_backend_raises_value_error(self):
        from vidushi_oa.backends import get_backend

        with self.assertRaises(ValueError):
            get_backend("bogus")

    def test_env_var_selects_backend(self):
        from vidushi_oa.backends import get_backend

        os.environ["VIDUSHI_BACKEND"] = "mongo"
        self.assertEqual(get_backend().name, "mongo")

    def test_backend_exposes_dup_error_and_check(self):
        from vidushi_oa.backends import get_backend

        be = get_backend("mongo")
        self.assertTrue(hasattr(be, "dup_error"), "backend must expose its duplicate-key error type")
        self.assertTrue(callable(getattr(be, "check", None)), "backend must expose a check() readiness probe")

    def test_cli_has_no_direct_pymongo(self):
        # §S1 caller-existence: pymongo lives ONLY under the mongo backend module, never _cli.py
        import vidushi_oa

        cli = os.path.join(os.path.dirname(vidushi_oa.__file__), "_cli.py")
        with open(cli, encoding="utf-8") as f:
            self.assertNotIn("pymongo", f.read(), "_cli.py must not reference pymongo directly")


class DefaultBackendTest(unittest.TestCase):
    """CR-OA-018 §S3 — SQLite becomes the default backend when none is explicitly selected."""

    def setUp(self):
        self._saved_backend_env = os.environ.pop("VIDUSHI_BACKEND", None)

    def tearDown(self):
        os.environ.pop("VIDUSHI_BACKEND", None)
        if self._saved_backend_env is not None:
            os.environ["VIDUSHI_BACKEND"] = self._saved_backend_env

    def test_default_backend_is_sqlite(self):
        from vidushi_oa.backends import get_backend

        be = get_backend()
        self.assertEqual(be.name, "sqlite")

    def test_explicit_mongo_still_selectable(self):
        from vidushi_oa.backends import get_backend

        be = get_backend("mongo")
        self.assertEqual(be.name, "mongo")

        os.environ["VIDUSHI_BACKEND"] = "mongo"
        self.assertEqual(get_backend().name, "mongo")


if __name__ == "__main__":
    unittest.main()
