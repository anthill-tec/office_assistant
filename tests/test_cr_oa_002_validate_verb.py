"""CR-OA-002 Cycle B — `store.py validate [<type>]` verb.

Verifies §S3: `validate` lists the non-conforming documents in a collection via
`find({"$nor": [{"$jsonSchema": <schema>}]})`, printing their ids as compact JSON.

  - §S3 `store.py validate invoices` returns `[]` against conforming data.
  - §S3 after inserting one deliberately bad doc (bypassing the validator), its
    `id` appears in the output list.
  - §S3 (caller) `validate` is a real subparser (not yet wired — this is the RED).

Runs entirely against a throwaway `office_assistant_test` database (OA_MONGO_DB env
override) so it never touches real data, and drops that database in tearDown.
Requires a local mongod on 127.0.0.1:27017 (same instance CR-OA-001/Cycle A pinned to).
"""
import json
import os
import subprocess
import sys
import unittest

import pymongo

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, "scripts")
STORE = os.path.join(SCRIPTS, "store.py")

TEST_DB = "office_assistant_test"


class ValidateVerbTest(unittest.TestCase):
    """§S3 — `store.py validate <type>` lists non-conforming document ids."""

    def setUp(self):
        self.env = dict(os.environ)
        self.env["OA_MONGO_DB"] = TEST_DB

        init_result = subprocess.run(
            [sys.executable, STORE, "init"],
            capture_output=True, text=True, env=self.env,
        )
        self.assertEqual(
            init_result.returncode, 0,
            f"store.py init failed against {TEST_DB}: {init_result.stderr}",
        )

        self.client = pymongo.MongoClient(
            "mongodb://127.0.0.1:27017", serverSelectionTimeoutMS=2000
        )

    def tearDown(self):
        self.client.drop_database(TEST_DB)
        self.client.close()

    def _run_validate(self, *args):
        return subprocess.run(
            [sys.executable, STORE, "validate", *args],
            capture_output=True, text=True, env=self.env,
        )

    def test_validate_returns_empty_for_conforming(self):
        coll = self.client[TEST_DB]["invoices"]
        doc = {
            "id": "doc_ok_1",
            "vendor": "X",
            "doc_type": "invoice",
            "date": "2026-01-01",
            "acct": "personal",
            "status": "COMPLETED",
        }
        insert_result = coll.insert_one(doc)
        self.assertTrue(insert_result.acknowledged)

        result = self._run_validate("invoices")

        self.assertEqual(
            result.returncode, 0,
            f"store.py validate invoices should exit 0, got {result.returncode}; "
            f"stderr: {result.stderr}",
        )
        parsed = json.loads(result.stdout)
        self.assertEqual(parsed, [])
        # negative bound: the conforming doc must never be reported
        self.assertNotIn("doc_ok_1", parsed)

    def test_validate_lists_nonconforming_id(self):
        coll = self.client[TEST_DB]["invoices"]
        good = {
            "id": "doc_ok_2",
            "vendor": "X",
            "doc_type": "invoice",
            "date": "2026-01-01",
            "acct": "personal",
            "status": "COMPLETED",
        }
        coll.insert_one(good)
        # bypass the collection validator to seed a genuinely non-conforming doc
        coll.insert_one(
            {"id": "doc_bad_1", "status": "BOGUS"},
            bypass_document_validation=True,
        )

        result = self._run_validate("invoices")

        self.assertEqual(
            result.returncode, 0,
            f"store.py validate invoices should exit 0, got {result.returncode}; "
            f"stderr: {result.stderr}",
        )
        parsed = json.loads(result.stdout)
        self.assertIn("doc_bad_1", parsed)
        # positive bound: exactly the one bad doc, the conforming one stays out
        self.assertEqual(parsed, ["doc_bad_1"])
        self.assertNotIn("doc_ok_2", parsed)


if __name__ == "__main__":
    unittest.main()
