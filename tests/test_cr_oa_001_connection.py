"""CR-OA-001 — MongoDB connection & collection bootstrap.

Verifies the ACs: connection helper defaults + env override, collection access,
and `store.py init` creating a unique `id` index per store (idempotently).
Requires a local mongod on 127.0.0.1:27017 (the office_assistant instance).
"""
import os
import sys
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, "scripts")
STORE = os.path.join(SCRIPTS, "store.py")
sys.path.insert(0, SCRIPTS)

import oa_mongo  # noqa: E402  (RED until CR-OA-001 ships the module)
import store     # noqa: E402


class TestConnection(unittest.TestCase):
    def test_db_defaults_to_office_assistant(self):
        self.assertEqual(oa_mongo.db().name, "vidushi_oa")

    def test_client_pinned_to_27017(self):
        oa_mongo.client().admin.command("ping")
        self.assertEqual(oa_mongo.client().address, ("127.0.0.1", 27017))

    def test_env_override_db_name(self):
        os.environ["VIDUSHI_MONGO_DB"] = "vidushi_oa_test"
        try:
            self.assertEqual(oa_mongo.db().name, "vidushi_oa_test")
        finally:
            del os.environ["VIDUSHI_MONGO_DB"]

    def test_stores_are_the_five_types(self):
        self.assertEqual(
            set(store.STORES),
            {"contacts", "invoices", "warranties", "cases", "products",
             "subscriptions", "insurance", "orders"},
        )

    def test_coll_targets_named_collection(self):
        c = oa_mongo.coll("invoices")
        self.assertEqual(c.name, "invoices")
        self.assertEqual(c.database.name, "vidushi_oa")

    def test_init_creates_unique_id_index_idempotently(self):
        r1 = subprocess.run([sys.executable, STORE, "init"], capture_output=True, text=True)
        self.assertEqual(r1.returncode, 0, r1.stderr)
        for t in store.STORES:
            idx = oa_mongo.coll(t).index_information()
            has_unique_id = any(
                v.get("unique") and v.get("key") == [("id", 1)] for v in idx.values()
            )
            self.assertTrue(has_unique_id, f"{t} missing unique id index: {idx}")
        r2 = subprocess.run([sys.executable, STORE, "init"], capture_output=True, text=True)
        self.assertEqual(r2.returncode, 0, r2.stderr)


if __name__ == "__main__":
    unittest.main()
