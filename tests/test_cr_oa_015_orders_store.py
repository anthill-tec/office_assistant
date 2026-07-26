"""CR-OA-015 Cycle A — §S1 register the `orders` store.

Verifies the §S1 acceptance criteria for the new fulfilment-lifecycle store:

  - `store.STORES` gains `orders` (snapshot file `orders.jsonl`) and `store.PREFIX` gains
    `orders` -> `ord`; an existing store (`invoices` -> `doc`) stays undisturbed.
  - `gen_id` anchors `orders` on `merchant` (not the default `vendor`) plus a
    `number|date` suffix, mirroring the `invoices` anchor-plus-number convention:
    `add orders --json '{"merchant":"Acme","number":"A1"}'` mints `ord_acme_a1`.
  - `vidushi_oa/schema/orders.schema.json` (the PACKAGED runtime schema, not
    `data/schema/`) encodes: `id.pattern == "^ord_"`; `status.enum ==
    ["NEW","UNKNOWN","IN_PROGRESS","COMPLETED"]` (exactly 4 -- no EXPIRED/DUE);
    `actions.items.properties.status.enum == ["OPEN","RESOLVED"]`.
  - After `store.py init`, the `orders` collection carries a `$jsonSchema` validator
    (rejects an out-of-enum status with `pymongo.errors.WriteError`, no write lands;
    accepts a valid status) and a `unique:true` `id` index (`DuplicateKeyError` on a
    repeat id).
  - `store.py validate orders` returns `[]` against a conforming seed row.
  - `store.py get orders <id> --expand invoice_id,product_id` resolves both FKs inline
    as `invoice_id_obj` / `product_id_obj` via the EXISTING `FK_MAP` entries -- no new
    FK_MAP key is added by this CR.

`orders` is not registered yet (CR-OA-015 Cycle A §S1 is still RED), so EVERY test here
MUST fail: the registry test via a `KeyError`/`AssertionError` on the not-yet-existing
entries; the schema test via `FileNotFoundError` (no packaged
`vidushi_oa/schema/orders.schema.json` yet); the subprocess-driven tests because
`store.py`'s argparse `choices=STORES.keys()` rejects `orders` as a `<type>` value (or,
for the validator/index tests, because no validator/index is attached and the bad/
duplicate insert therefore succeeds where it should raise).

DATA SAFETY: every subprocess call points `VIDUSHI_DATA_DIR` at an EMPTY tempdir (never
the real repo `data/`) and `VIDUSHI_MONGO_DB` at `vidushi_oa_test` (never the real DB),
dropped in tearDown. Requires a local mongod on 127.0.0.1:27017 (the office_assistant
instance; CR-OA-001).
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import pymongo
import pymongo.errors

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, "scripts")
STORE = os.path.join(SCRIPTS, "store.py")
# The PACKAGED runtime schema dir -- validators load from here (`_load_schema` in
# vidushi_oa/_cli.py), NOT from data/schema/. This CR's AC is explicit about the path.
PACKAGE_SCHEMA_DIR = os.path.join(ROOT, "vidushi_oa", "schema")

TEST_DB = "vidushi_oa_test"


def _load_packaged_schema(type_name):
    path = os.path.join(PACKAGE_SCHEMA_DIR, f"{type_name}.schema.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class RegistryTest(unittest.TestCase):
    """§S1 -- `store.STORES`/`store.PREFIX` gain the `orders` entry (pure module
    import, no Mongo needed)."""

    def test_orders_registered_with_ord_prefix(self):
        sys.path.insert(0, SCRIPTS)
        import store

        self.assertIn("orders", store.STORES)
        self.assertEqual(store.PREFIX.get("orders"), "ord")
        self.assertEqual(store.STORES.get("orders"), "orders.jsonl")
        # negative bound: registering orders must not disturb an existing store
        self.assertEqual(store.PREFIX.get("invoices"), "doc")


class GenIdAnchorTest(unittest.TestCase):
    """§S1 -- `store.py add orders` anchors the new id on `merchant` (+ number
    suffix), NOT the default `vendor`, mirroring the invoices anchor-plus-number
    convention."""

    def setUp(self):
        # An EMPTY data dir: `add` writes to Mongo only, proving the verb is not
        # silently reading/writing the real data/*.jsonl files.
        self.data_dir = tempfile.mkdtemp(prefix="oa-cr015-data-")

        self.env = dict(os.environ)
        self.env["VIDUSHI_MONGO_DB"] = TEST_DB
        self.env["VIDUSHI_DATA_DIR"] = self.data_dir
        self.env["VIDUSHI_FORMAT"] = "json"

        self.client = pymongo.MongoClient("mongodb://127.0.0.1:27017", serverSelectionTimeoutMS=2000)
        self.db = self.client[TEST_DB]

    def tearDown(self):
        self.client.drop_database(TEST_DB)
        self.client.close()
        shutil.rmtree(self.data_dir, ignore_errors=True)

    def _add(self, type_name, json_payload):
        return subprocess.run(
            [sys.executable, STORE, "add", type_name, "--json", json_payload],
            capture_output=True, text=True, env=self.env,
        )

    def test_add_order_generates_merchant_number_anchor_id(self):
        result = self._add("orders", json.dumps({"merchant": "Acme", "number": "A1"}))

        self.assertEqual(
            result.returncode, 0,
            f"store.py add orders failed (rc={result.returncode}): {result.stderr}",
        )
        payload = json.loads(result.stdout.strip())
        self.assertEqual(payload.get("added"), ["ord_acme_a1"])
        # negative bound: nothing was skipped as a dupe on a fresh DB
        self.assertEqual(payload.get("skipped"), [])

        doc = self.db["orders"].find_one({"id": "ord_acme_a1"})
        self.assertIsNotNone(doc, "ord_acme_a1 must exist in the Mongo test DB")
        self.assertEqual(doc["merchant"], "Acme")
        self.assertEqual(doc["number"], "A1")


class OrdersSchemaTest(unittest.TestCase):
    """§S1 -- vidushi_oa/schema/orders.schema.json (the PACKAGED runtime schema)
    encodes the domain enums + id pattern exactly."""

    def test_id_pattern_is_ord_prefix(self):
        schema = _load_packaged_schema("orders")
        self.assertEqual(schema["properties"]["id"]["pattern"], "^ord_")

    def test_status_enum_is_exactly_four_no_expired_or_due(self):
        schema = _load_packaged_schema("orders")
        self.assertEqual(
            schema["properties"]["status"]["enum"],
            ["NEW", "UNKNOWN", "IN_PROGRESS", "COMPLETED"],
        )
        # negative bound: no EXPIRED/DUE (unlike warranties/subscriptions/insurance)
        self.assertEqual(len(schema["properties"]["status"]["enum"]), 4)
        self.assertNotIn("EXPIRED", schema["properties"]["status"]["enum"])
        self.assertNotIn("DUE", schema["properties"]["status"]["enum"])

    def test_alias_is_free_string_and_acct_is_personal_business_enum(self):
        # Guard the alias/acct split (DN-purchases-persistence): `alias` is the masked
        # buying alias (a free string), `acct` is the personal|business ledger split --
        # they are distinct fields and must not be conflated.
        schema = _load_packaged_schema("orders")
        props = schema["properties"]
        self.assertEqual(props["alias"].get("bsonType"), "string")
        self.assertNotIn("enum", props["alias"], "alias is a free string, not an enum")
        self.assertEqual(props["acct"].get("enum"), ["personal", "business"])

    def test_actions_item_status_enum_is_open_resolved_only(self):
        schema = _load_packaged_schema("orders")
        self.assertEqual(
            schema["properties"]["actions"]["items"]["properties"]["status"]["enum"],
            ["OPEN", "RESOLVED"],
        )
        self.assertEqual(
            len(schema["properties"]["actions"]["items"]["properties"]["status"]["enum"]), 2
        )


class ValidatorEnforcedTest(unittest.TestCase):
    """§S1 -- `apply-validators` (via `store.py init`) attaches a Mongo $jsonSchema
    validator to `orders` that rejects an out-of-enum status and accepts a valid one,
    against the throwaway `vidushi_oa_test` DB. Also verifies the unique `id` index."""

    def setUp(self):
        self.env = dict(os.environ)
        self.env["VIDUSHI_MONGO_DB"] = TEST_DB
        self.env["VIDUSHI_FORMAT"] = "json"
        result = subprocess.run(
            [sys.executable, STORE, "init"], capture_output=True, text=True, env=self.env,
        )
        self.assertEqual(
            result.returncode, 0,
            f"store.py init failed against {TEST_DB}: {result.stderr}",
        )
        self.client = pymongo.MongoClient("mongodb://127.0.0.1:27017", serverSelectionTimeoutMS=2000)
        self.db = self.client[TEST_DB]

    def tearDown(self):
        self.client.drop_database(TEST_DB)
        self.client.close()

    def test_orders_validator_rejects_bad_status_and_accepts_valid(self):
        coll = self.db["orders"]
        with self.assertRaises(pymongo.errors.WriteError):
            coll.insert_one({"id": "ord_bad", "status": "delivered"})
        # negative bound: the rejected document must not have landed in the collection
        self.assertIsNone(coll.find_one({"id": "ord_bad"}))

        result = coll.insert_one({"id": "ord_ok", "status": "IN_PROGRESS"})
        self.assertTrue(result.acknowledged)
        stored = coll.find_one({"id": "ord_ok"})
        self.assertIsNotNone(stored)
        self.assertEqual(stored["status"], "IN_PROGRESS")
        self.assertEqual(coll.count_documents({}), 1)

    def test_init_creates_unique_id_index_on_orders(self):
        coll = self.db["orders"]
        coll.insert_one({"id": "ord_dup", "status": "IN_PROGRESS"})
        with self.assertRaises(pymongo.errors.DuplicateKeyError):
            coll.insert_one({"id": "ord_dup", "status": "COMPLETED"})
        # negative bound: only the first insert landed
        self.assertEqual(coll.count_documents({"id": "ord_dup"}), 1)


class ValidateVerbTest(unittest.TestCase):
    """§S1 -- `store.py validate orders` returns `[]` against conforming seed data
    (seeded directly via pymongo, avoiding a dependency on `add`'s own RED state)."""

    def setUp(self):
        self.env = dict(os.environ)
        self.env["VIDUSHI_MONGO_DB"] = TEST_DB
        self.env["VIDUSHI_FORMAT"] = "json"
        result = subprocess.run(
            [sys.executable, STORE, "init"], capture_output=True, text=True, env=self.env,
        )
        self.assertEqual(
            result.returncode, 0,
            f"store.py init failed against {TEST_DB}: {result.stderr}",
        )
        self.client = pymongo.MongoClient("mongodb://127.0.0.1:27017", serverSelectionTimeoutMS=2000)
        self.db = self.client[TEST_DB]

    def tearDown(self):
        self.client.drop_database(TEST_DB)
        self.client.close()

    def test_validate_orders_returns_empty_for_conforming(self):
        self.db["orders"].insert_one(
            {"id": "ord_conform", "merchant": "Acme", "status": "IN_PROGRESS"}
        )

        result = subprocess.run(
            [sys.executable, STORE, "validate", "orders"],
            capture_output=True, text=True, env=self.env,
        )

        self.assertEqual(
            result.returncode, 0,
            f"store.py validate orders should exit 0, got {result.returncode}; "
            f"stderr: {result.stderr}",
        )
        parsed = json.loads(result.stdout)
        self.assertEqual(parsed, [])
        self.assertNotIn("ord_conform", parsed)


class OrdersExpandFkTest(unittest.TestCase):
    """§S1 -- `get orders <id> --expand invoice_id,product_id` resolves both FKs
    inline via the EXISTING FK_MAP entries (`invoice_id` -> invoices, `product_id`
    -> products); no new FK_MAP key is required by this CR."""

    def setUp(self):
        self.env = dict(os.environ)
        self.env["VIDUSHI_MONGO_DB"] = TEST_DB
        self.env["VIDUSHI_FORMAT"] = "json"
        self.client = pymongo.MongoClient("mongodb://127.0.0.1:27017", serverSelectionTimeoutMS=2000)
        self.db = self.client[TEST_DB]

    def tearDown(self):
        self.client.drop_database(TEST_DB)
        self.client.close()

    def test_order_expand_invoice_and_product_fks_resolve_inline(self):
        self.db["invoices"].insert_one({"id": "doc_acme_1", "vendor": "Acme"})
        self.db["products"].insert_one({"id": "prod_acme_widget", "manufacturer": "Acme"})
        self.db["orders"].insert_one(
            {
                "id": "ord_acme_a1",
                "merchant": "Acme",
                "invoice_id": "doc_acme_1",
                "product_id": "prod_acme_widget",
            }
        )

        result = subprocess.run(
            [sys.executable, STORE, "get", "orders", "ord_acme_a1",
             "--expand", "invoice_id,product_id"],
            capture_output=True, text=True, env=self.env,
        )
        self.assertEqual(
            result.returncode, 0,
            f"store.py get orders --expand invoice_id,product_id failed: {result.stderr}",
        )
        payload = json.loads(result.stdout.strip())

        self.assertIn("invoice_id_obj", payload)
        self.assertIsNotNone(payload["invoice_id_obj"])
        self.assertEqual(payload["invoice_id_obj"].get("id"), "doc_acme_1")
        self.assertEqual(payload["invoice_id_obj"].get("vendor"), "Acme")

        self.assertIn("product_id_obj", payload)
        self.assertIsNotNone(payload["product_id_obj"])
        self.assertEqual(payload["product_id_obj"].get("id"), "prod_acme_widget")
        self.assertEqual(payload["product_id_obj"].get("manufacturer"), "Acme")

        # negative bound: the raw FK fields are untouched alongside the expanded objects
        self.assertEqual(payload.get("invoice_id"), "doc_acme_1")
        self.assertEqual(payload.get("product_id"), "prod_acme_widget")


if __name__ == "__main__":
    unittest.main()
