"""CR-OA-007 Cycle A — register the `subscriptions` + `insurance` stores.

Verifies the §S1 ACs for the new recurring-domain stores:

  - `store.STORES` gains `subscriptions` (prefix `sub`) and `insurance` (prefix `ins`);
    `store.FK_MAP["subscription_id"] == "subscriptions"` so `get invoices <id>
    --expand subscription_id` resolves it inline.
  - `data/schema/subscriptions.schema.json` + `data/schema/insurance.schema.json` encode
    the shared `status`/`actions[].status` enums and an id `pattern` anchored to their
    prefix.
  - `gen_id` anchors: subscriptions on `provider`, insurance on `insurer` (+ `policy_no`
    suffix, mirroring the `invoices` anchor-plus-number convention).
  - After `store.py init`, both collections carry a unique `id` index and a `$jsonSchema`
    validator (out-of-enum `status` rejected, a conforming doc accepted, duplicate `id`
    rejected).
  - `store.py validate subscriptions` / `validate insurance` return `[]` against
    conforming seed data.

Neither store is registered yet (CR-OA-007 Cycle A is still RED), so EVERY test here MUST
fail: the registry/schema tests via KeyError/FileNotFoundError/AssertionError on the
not-yet-existing entries, and the subprocess-driven tests because `store.py`'s argparse
`choices=STORES.keys()` rejects `subscriptions`/`insurance` as a `<type>` value (or, for
the validator/index tests, because no validator/index is attached and the bad/duplicate
insert therefore succeeds where it should raise).

DATA SAFETY: every subprocess call points `OA_DATA_DIR` at an EMPTY tempdir (never the
real repo `data/`) and `OA_MONGO_DB` at `office_assistant_test` (never the real DB), which
is dropped in tearDown. Requires a local mongod on 127.0.0.1:27017 (the office_assistant
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
SCHEMA_DIR = os.path.join(ROOT, "data", "schema")

TEST_DB = "office_assistant_test"


def _load_schema(type_name):
    with open(os.path.join(SCHEMA_DIR, f"{type_name}.schema.json"), encoding="utf-8") as f:
        return json.load(f)


class RegistryTest(unittest.TestCase):
    """§S1 — `store.STORES`/`store.PREFIX`/`store.FK_MAP` gain the new entries (pure
    module import, no Mongo needed)."""

    def test_subscriptions_registered_with_sub_prefix(self):
        sys.path.insert(0, SCRIPTS)
        import store

        self.assertIn("subscriptions", store.STORES)
        self.assertEqual(store.PREFIX.get("subscriptions"), "sub")
        # negative bound: registering subscriptions must not disturb an existing store
        self.assertEqual(store.PREFIX.get("invoices"), "doc")

    def test_insurance_registered_with_ins_prefix(self):
        sys.path.insert(0, SCRIPTS)
        import store

        self.assertIn("insurance", store.STORES)
        self.assertEqual(store.PREFIX.get("insurance"), "ins")
        # negative bound: registering insurance must not disturb an existing store
        self.assertEqual(store.PREFIX.get("warranties"), "war")

    def test_fk_map_subscription_id_points_to_subscriptions_store(self):
        sys.path.insert(0, SCRIPTS)
        import store

        self.assertEqual(store.FK_MAP.get("subscription_id"), "subscriptions")
        # negative bound: the new FK must not collide with / overwrite an existing one
        self.assertEqual(store.FK_MAP.get("invoice_id"), "invoices")


class SubscriptionsSchemaTest(unittest.TestCase):
    """§S1 — data/schema/subscriptions.schema.json encodes the domain enums + id pattern."""

    def test_id_pattern_is_sub_prefix(self):
        schema = _load_schema("subscriptions")
        self.assertEqual(schema["properties"]["id"]["pattern"], "^sub_")

    def test_status_enum_matches_domain_vocabulary(self):
        schema = _load_schema("subscriptions")
        self.assertEqual(
            schema["properties"]["status"]["enum"],
            ["NEW", "UNKNOWN", "IN_PROGRESS", "COMPLETED", "EXPIRED", "DUE"],
        )
        # negative bound: no extra/renamed status sneaked in
        self.assertEqual(len(schema["properties"]["status"]["enum"]), 6)

    def test_actions_item_status_enum_is_open_resolved_only(self):
        schema = _load_schema("subscriptions")
        self.assertEqual(
            schema["properties"]["actions"]["items"]["properties"]["status"]["enum"],
            ["OPEN", "RESOLVED"],
        )
        self.assertEqual(
            len(schema["properties"]["actions"]["items"]["properties"]["status"]["enum"]), 2
        )


class InsuranceSchemaTest(unittest.TestCase):
    """§S1 — data/schema/insurance.schema.json encodes the domain enums + id pattern."""

    def test_id_pattern_is_ins_prefix(self):
        schema = _load_schema("insurance")
        self.assertEqual(schema["properties"]["id"]["pattern"], "^ins_")

    def test_status_enum_matches_domain_vocabulary(self):
        schema = _load_schema("insurance")
        self.assertEqual(
            schema["properties"]["status"]["enum"],
            ["NEW", "UNKNOWN", "IN_PROGRESS", "COMPLETED", "EXPIRED", "DUE"],
        )
        self.assertEqual(len(schema["properties"]["status"]["enum"]), 6)

    def test_actions_item_status_enum_is_open_resolved_only(self):
        schema = _load_schema("insurance")
        self.assertEqual(
            schema["properties"]["actions"]["items"]["properties"]["status"]["enum"],
            ["OPEN", "RESOLVED"],
        )
        self.assertEqual(
            len(schema["properties"]["actions"]["items"]["properties"]["status"]["enum"]), 2
        )


class GenIdAnchorTest(unittest.TestCase):
    """§S1 — `store.py add` anchors new-store ids on the right field via `gen_id`."""

    def setUp(self):
        # An EMPTY data dir: `add` writes to Mongo only, but this proves the verb is
        # not silently reading/writing the real data/*.jsonl files.
        self.data_dir = tempfile.mkdtemp(prefix="oa-cr007-data-")

        self.env = dict(os.environ)
        self.env["OA_MONGO_DB"] = TEST_DB
        self.env["OA_DATA_DIR"] = self.data_dir
        self.env["OA_FORMAT"] = "json"

        self.client = pymongo.MongoClient("mongodb://127.0.0.1:27017", serverSelectionTimeoutMS=2000)
        self.db = self.client[TEST_DB]

    def tearDown(self):
        self.client.drop_database(TEST_DB)
        self.client.close()
        shutil.rmtree(self.data_dir, ignore_errors=True)

    def _add(self, type_name, json_payload):
        result = subprocess.run(
            [sys.executable, STORE, "add", type_name, "--json", json_payload],
            capture_output=True, text=True, env=self.env,
        )
        return result

    def test_add_subscription_generates_provider_anchor_id(self):
        result = self._add("subscriptions", json.dumps({"provider": "Acme"}))

        self.assertEqual(
            result.returncode, 0,
            f"store.py add subscriptions failed (rc={result.returncode}): {result.stderr}",
        )
        payload = json.loads(result.stdout.strip())
        self.assertEqual(payload.get("added"), ["sub_acme"])
        # negative bound: nothing was skipped as a dupe on a fresh DB
        self.assertEqual(payload.get("skipped"), [])

        doc = self.db["subscriptions"].find_one({"id": "sub_acme"})
        self.assertIsNotNone(doc, "sub_acme must exist in the Mongo test DB")
        self.assertEqual(doc["provider"], "Acme")

    def test_add_insurance_generates_insurer_policy_anchor_id(self):
        result = self._add(
            "insurance", json.dumps({"insurer": "HDFC Ergo", "policy_no": "P123"})
        )

        self.assertEqual(
            result.returncode, 0,
            f"store.py add insurance failed (rc={result.returncode}): {result.stderr}",
        )
        payload = json.loads(result.stdout.strip())
        self.assertEqual(len(payload.get("added", [])), 1)
        new_id = payload["added"][0]
        self.assertTrue(
            new_id.startswith("ins_hdfc-ergo"),
            f"expected id anchored on insurer 'HDFC Ergo', got {new_id!r}",
        )
        self.assertEqual(payload.get("skipped"), [])

        doc = self.db["insurance"].find_one({"id": new_id})
        self.assertIsNotNone(doc, f"{new_id} must exist in the Mongo test DB")
        self.assertEqual(doc["insurer"], "HDFC Ergo")
        self.assertEqual(doc["policy_no"], "P123")


class ValidatorEnforcedTest(unittest.TestCase):
    """§S1 — `apply-validators` (via `store.py init`) attaches a Mongo $jsonSchema
    validator to `subscriptions`/`insurance` that rejects an out-of-enum status and
    accepts a valid one, entirely against the throwaway `office_assistant_test` DB."""

    def setUp(self):
        self.env = dict(os.environ)
        self.env["OA_MONGO_DB"] = TEST_DB
        self.env["OA_FORMAT"] = "json"
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

    def test_subscriptions_validator_rejects_bad_status_and_accepts_valid(self):
        coll = self.db["subscriptions"]
        with self.assertRaises(pymongo.errors.WriteError):
            coll.insert_one({"id": "sub_bad", "status": "BOGUS"})
        # negative bound: the rejected document must not have landed in the collection
        self.assertIsNone(coll.find_one({"id": "sub_bad"}))

        result = coll.insert_one({"id": "sub_ok", "status": "IN_PROGRESS"})
        self.assertTrue(result.acknowledged)
        stored = coll.find_one({"id": "sub_ok"})
        self.assertIsNotNone(stored)
        self.assertEqual(stored["status"], "IN_PROGRESS")
        self.assertEqual(coll.count_documents({}), 1)

    def test_insurance_validator_rejects_bad_status_and_accepts_valid(self):
        coll = self.db["insurance"]
        with self.assertRaises(pymongo.errors.WriteError):
            coll.insert_one({"id": "ins_bad", "status": "BOGUS"})
        self.assertIsNone(coll.find_one({"id": "ins_bad"}))

        result = coll.insert_one({"id": "ins_ok", "status": "IN_PROGRESS"})
        self.assertTrue(result.acknowledged)
        stored = coll.find_one({"id": "ins_ok"})
        self.assertIsNotNone(stored)
        self.assertEqual(stored["status"], "IN_PROGRESS")
        self.assertEqual(coll.count_documents({}), 1)

    def test_init_creates_unique_id_index_on_subscriptions(self):
        coll = self.db["subscriptions"]
        coll.insert_one({"id": "sub_dup", "status": "IN_PROGRESS"})
        with self.assertRaises(pymongo.errors.DuplicateKeyError):
            coll.insert_one({"id": "sub_dup", "status": "COMPLETED"})
        # negative bound: only the first insert landed
        self.assertEqual(coll.count_documents({"id": "sub_dup"}), 1)

    def test_init_creates_unique_id_index_on_insurance(self):
        coll = self.db["insurance"]
        coll.insert_one({"id": "ins_dup", "status": "IN_PROGRESS"})
        with self.assertRaises(pymongo.errors.DuplicateKeyError):
            coll.insert_one({"id": "ins_dup", "status": "COMPLETED"})
        self.assertEqual(coll.count_documents({"id": "ins_dup"}), 1)


class ValidateVerbTest(unittest.TestCase):
    """§S1 — `store.py validate subscriptions` / `validate insurance` return `[]`
    against conforming seed data (seeded directly via pymongo to avoid depending on
    `add`'s own RED state)."""

    def setUp(self):
        self.env = dict(os.environ)
        self.env["OA_MONGO_DB"] = TEST_DB
        self.env["OA_FORMAT"] = "json"
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

    def _validate(self, type_name):
        return subprocess.run(
            [sys.executable, STORE, "validate", type_name],
            capture_output=True, text=True, env=self.env,
        )

    def test_validate_subscriptions_returns_empty_for_conforming(self):
        self.db["subscriptions"].insert_one(
            {"id": "sub_conform", "provider": "Acme", "status": "IN_PROGRESS"}
        )

        result = self._validate("subscriptions")

        self.assertEqual(
            result.returncode, 0,
            f"store.py validate subscriptions should exit 0, got {result.returncode}; "
            f"stderr: {result.stderr}",
        )
        parsed = json.loads(result.stdout)
        self.assertEqual(parsed, [])
        self.assertNotIn("sub_conform", parsed)

    def test_validate_insurance_returns_empty_for_conforming(self):
        self.db["insurance"].insert_one(
            {"id": "ins_conform", "insurer": "HDFC Ergo", "status": "IN_PROGRESS"}
        )

        result = self._validate("insurance")

        self.assertEqual(
            result.returncode, 0,
            f"store.py validate insurance should exit 0, got {result.returncode}; "
            f"stderr: {result.stderr}",
        )
        parsed = json.loads(result.stdout)
        self.assertEqual(parsed, [])
        self.assertNotIn("ins_conform", parsed)


class InvoiceExpandSubscriptionFkTest(unittest.TestCase):
    """§S1 (caller AC) — `get invoices <id> --expand subscription_id` resolves the FK
    inline as `subscription_id_obj`, proving `FK_MAP["subscription_id"]` is wired all
    the way through `expand()`, not just present as a dict key."""

    def setUp(self):
        self.env = dict(os.environ)
        self.env["OA_MONGO_DB"] = TEST_DB
        self.env["OA_FORMAT"] = "json"
        self.client = pymongo.MongoClient("mongodb://127.0.0.1:27017", serverSelectionTimeoutMS=2000)
        self.db = self.client[TEST_DB]

    def tearDown(self):
        self.client.drop_database(TEST_DB)
        self.client.close()

    def test_invoice_expand_subscription_id_resolves_to_subscription_doc(self):
        self.db["subscriptions"].insert_one(
            {"id": "sub_acme", "provider": "Acme", "status": "IN_PROGRESS"}
        )
        self.db["invoices"].insert_one(
            {"id": "doc_acme_1", "vendor": "Acme", "subscription_id": "sub_acme"}
        )

        result = subprocess.run(
            [sys.executable, STORE, "get", "invoices", "doc_acme_1", "--expand", "subscription_id"],
            capture_output=True, text=True, env=self.env,
        )
        self.assertEqual(
            result.returncode, 0,
            f"store.py get invoices --expand subscription_id failed: {result.stderr}",
        )
        payload = json.loads(result.stdout.strip())
        self.assertIn("subscription_id_obj", payload)
        self.assertIsNotNone(payload["subscription_id_obj"])
        self.assertEqual(payload["subscription_id_obj"].get("id"), "sub_acme")
        self.assertEqual(payload["subscription_id_obj"].get("provider"), "Acme")
        # negative bound: the raw FK field is untouched alongside the expanded object
        self.assertEqual(payload.get("subscription_id"), "sub_acme")


if __name__ == "__main__":
    unittest.main()
