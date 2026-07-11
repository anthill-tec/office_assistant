"""CR-OA-002 Cycle A — domain JSON-Schema files + Mongo `$jsonSchema` validators.

Verifies the ACs:
  A1 (S1) invoices.schema.json encodes the status/actions enums + id pattern.
  A2 (S1) products.schema.json encodes the kind/relation/billing enums.
  A3 (S2) `apply-validators` (via `store.py init`) makes the `invoices` collection
          reject a document with an out-of-enum `status` (pymongo.errors.WriteError)
          while a schema-valid invoice inserts cleanly.

A3 runs against a throwaway `office_assistant_test` database (OA_MONGO_DB env override)
so it never touches the real `office_assistant` data, and drops that database in
teardown. Requires a local mongod on 127.0.0.1:27017 (same instance CR-OA-001 pinned to).
"""
import json
import os
import subprocess
import sys
import unittest

import pymongo.errors

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, "scripts")
DATA = os.path.join(ROOT, "data")
STORE = os.path.join(SCRIPTS, "store.py")
SCHEMA_DIR = os.path.join(DATA, "schema")
sys.path.insert(0, SCRIPTS)

import oa_mongo  # noqa: E402


def _load_schema(type_name):
    schema_path = os.path.join(SCHEMA_DIR, f"{type_name}.schema.json")
    with open(schema_path, encoding="utf-8") as f:
        return json.load(f)


class InvoicesSchemaEnumsTest(unittest.TestCase):
    """A1 — data/schema/invoices.schema.json encodes the domain enums + id pattern."""

    def test_status_enum_matches_domain_vocabulary(self):
        schema = _load_schema("invoices")
        self.assertEqual(
            schema["properties"]["status"]["enum"],
            ["NEW", "UNKNOWN", "IN_PROGRESS", "COMPLETED", "EXPIRED", "DUE"],
        )
        # negative bound: no extra/renamed status sneaked in
        self.assertEqual(len(schema["properties"]["status"]["enum"]), 6)

    def test_actions_item_status_enum_is_open_resolved_only(self):
        schema = _load_schema("invoices")
        self.assertEqual(
            schema["properties"]["actions"]["items"]["properties"]["status"]["enum"],
            ["OPEN", "RESOLVED"],
        )
        # negative bound: exactly two action states, nothing else
        self.assertEqual(
            len(schema["properties"]["actions"]["items"]["properties"]["status"]["enum"]), 2
        )

    def test_id_pattern_anchors_doc_prefix(self):
        schema = _load_schema("invoices")
        self.assertEqual(schema["properties"]["id"]["pattern"], "^doc_")


class ProductsSchemaEnumsTest(unittest.TestCase):
    """A2 — data/schema/products.schema.json encodes the catalogue enums."""

    def test_kind_enum_physical_virtual(self):
        schema = _load_schema("products")
        self.assertEqual(schema["properties"]["kind"]["enum"], ["physical", "virtual"])

    def test_relation_enum_accessory_consumable(self):
        schema = _load_schema("products")
        self.assertEqual(
            schema["properties"]["relation"]["enum"], ["accessory", "consumable"]
        )

    def test_billing_enum_one_time_subscription(self):
        schema = _load_schema("products")
        self.assertEqual(
            schema["properties"]["billing"]["enum"], ["one-time", "subscription"]
        )


class ValidatorEnforcedTest(unittest.TestCase):
    """A3 (S2) — `apply-validators` (via `store.py init`) attaches a Mongo $jsonSchema
    validator to `invoices` that rejects an out-of-enum status and accepts a valid one.

    Runs entirely against a throwaway `office_assistant_test` database so it never
    touches real data.
    """

    TEST_DB = "office_assistant_test"

    def setUp(self):
        env = dict(os.environ)
        env["OA_MONGO_DB"] = self.TEST_DB
        result = subprocess.run(
            [sys.executable, STORE, "init"],
            capture_output=True, text=True, env=env,
        )
        self.assertEqual(
            result.returncode, 0,
            f"store.py init failed against {self.TEST_DB}: {result.stderr}",
        )
        self.client = pymongo.MongoClient(
            "mongodb://127.0.0.1:27017", serverSelectionTimeoutMS=2000
        )

    def tearDown(self):
        self.client.drop_database(self.TEST_DB)
        self.client.close()

    def test_invalid_status_rejected_by_validator(self):
        coll = self.client[self.TEST_DB]["invoices"]
        with self.assertRaises(pymongo.errors.WriteError):
            coll.insert_one({"id": "doc_test_1", "vendor": "X", "status": "BOGUS"})
        # negative: the rejected document must not have landed in the collection
        self.assertIsNone(coll.find_one({"id": "doc_test_1"}))

    def test_valid_invoice_inserts_cleanly(self):
        coll = self.client[self.TEST_DB]["invoices"]
        doc = {
            "id": "doc_test_2",
            "vendor": "X",
            "doc_type": "invoice",
            "date": "2026-01-01",
            "acct": "personal",
            "status": "COMPLETED",
        }
        result = coll.insert_one(doc)
        self.assertTrue(result.acknowledged)
        stored = coll.find_one({"id": "doc_test_2"})
        self.assertIsNotNone(stored)
        self.assertEqual(stored["status"], "COMPLETED")
        self.assertEqual(coll.count_documents({}), 1)


if __name__ == "__main__":
    unittest.main()
