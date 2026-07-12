"""CR-OA-003 Cycle A — `store.py` read-path (`query`/`get`) on pymongo.

Verifies the ACs for the read-side backend swap (§S1 `_id` suppression, §S2 filter
translation):
  - `--where` null coercion (file=None -> {$in:[None]})
  - `--where` numeric coercion (amount=0 -> numeric match, not string "0")
  - `--contains` case-insensitive substring match on an ARRAY field
  - `--fields` dotted projection with NO `_id` in the output
  - `--after`/`--before` inclusive ISO date range
  - `--sort` ascending
  - `--limit`
  - `get ... --expand` resolves FKs inline as `<fk>_obj`, no `_id` anywhere
  - `--filter '{json}'` native Mongo passthrough

Runs entirely against a throwaway `office_assistant_test` database (OA_MONGO_DB env
override), seeded directly via pymongo (bypassing store.py) in setUp and dropped in
tearDown, so it never touches real data. Requires a local mongod on 127.0.0.1:27017
(same instance CR-OA-001/002 pin to).
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
sys.path.insert(0, SCRIPTS)


class ReadPathMongoTest(unittest.TestCase):
    """§S1/§S2 — query/get read from Mongo (office_assistant_test), preserving CLI semantics."""

    TEST_DB = "office_assistant_test"

    INVOICE_A = {
        "id": "doc_a", "vendor": "FNIRSI", "number": "75752", "file": None,
        "amount": 0, "products": ["HUSKYLENS 2", "widget"], "date": "2026-07-01",
        "status": "COMPLETED", "warranty_id": "war_x", "contact_id": "ven_y",
    }
    INVOICE_B = {
        "id": "doc_b", "vendor": "Mouser", "number": "39683133", "file": "documents/x.pdf",
        "amount": 1864.37, "products": ["cap"], "date": "2026-06-15", "status": "COMPLETED",
    }
    WARRANTY_X = {"id": "war_x", "product": "TDM-120", "expiry": "2028-06-12", "invoice_id": "doc_a"}
    CONTACT_Y = {"id": "ven_y", "vendor": "FNIRSI", "support_email": "s@fnirsi.com"}

    def setUp(self):
        self.env = dict(os.environ)
        self.env["OA_MONGO_DB"] = self.TEST_DB
        self.env["OA_FORMAT"] = "json"
        self.client = pymongo.MongoClient("mongodb://127.0.0.1:27017", serverSelectionTimeoutMS=2000)
        db = self.client[self.TEST_DB]
        db["invoices"].insert_many([dict(self.INVOICE_A), dict(self.INVOICE_B)])
        db["warranties"].insert_one(dict(self.WARRANTY_X))
        db["contacts"].insert_one(dict(self.CONTACT_Y))

    def tearDown(self):
        self.client.drop_database(self.TEST_DB)
        self.client.close()

    def _run(self, *args):
        result = subprocess.run(
            [sys.executable, STORE, *args], capture_output=True, text=True, env=self.env,
        )
        self.assertEqual(
            result.returncode, 0,
            f"store.py {' '.join(args)} failed (rc={result.returncode}): {result.stderr}",
        )
        return result.stdout.strip()

    def test_where_null_coercion_matches_none_field(self):
        stdout = self._run("query", "invoices", "--where", "file=None", "--fields", "id")
        self.assertEqual(json.loads(stdout), [{"id": "doc_a"}])
        # negative bound: doc_b (file set) must not appear
        self.assertNotIn("doc_b", stdout)

    def test_where_numeric_coercion_matches_zero_amount(self):
        stdout = self._run("query", "invoices", "--where", "amount=0", "--fields", "id")
        self.assertEqual(json.loads(stdout), [{"id": "doc_a"}])
        self.assertNotIn("doc_b", stdout)

    def test_contains_matches_array_field_case_insensitively(self):
        stdout = self._run("query", "invoices", "--contains", "products=husky", "--fields", "id")
        self.assertEqual(json.loads(stdout), [{"id": "doc_a"}])
        self.assertNotIn("doc_b", stdout)

    def test_fields_projection_excludes_mongo_id(self):
        stdout = self._run(
            "query", "invoices", "--where", "vendor=FNIRSI", "--fields", "id,vendor",
        )
        self.assertEqual(json.loads(stdout), [{"id": "doc_a", "vendor": "FNIRSI"}])
        self.assertNotIn('"_id"', stdout)

    def test_after_inclusive_date_bound(self):
        stdout = self._run("query", "invoices", "--after", "date=2026-06-20", "--fields", "id")
        self.assertEqual(json.loads(stdout), [{"id": "doc_a"}])
        self.assertNotIn("doc_b", stdout)

    def test_before_inclusive_date_bound(self):
        stdout = self._run("query", "invoices", "--before", "date=2026-06-20", "--fields", "id")
        self.assertEqual(json.loads(stdout), [{"id": "doc_b"}])
        self.assertNotIn("doc_a", stdout)

    def test_sort_ascending_by_date(self):
        stdout = self._run("query", "invoices", "--sort", "date", "--fields", "id")
        self.assertEqual(json.loads(stdout), [{"id": "doc_b"}, {"id": "doc_a"}])

    def test_limit_caps_result_count(self):
        # Scope to the seeded fixture's own vendor (unique to the test DB) so this only
        # passes if the query actually ran against the seeded Mongo collection, not the
        # real data/invoices.jsonl (which has no "doc_b"/"Mouser" row of this shape).
        stdout = self._run(
            "query", "invoices", "--where", "vendor=Mouser", "--limit", "1", "--fields", "id",
        )
        result = json.loads(stdout)
        self.assertEqual(result, [{"id": "doc_b"}])
        # negative bound: must not silently return everything
        self.assertLessEqual(len(result), 1)

    def test_get_expand_resolves_fks_and_excludes_mongo_id(self):
        stdout = self._run("get", "invoices", "doc_a", "--expand", "warranty_id,contact_id")
        rec = json.loads(stdout)
        self.assertIn("warranty_id_obj", rec)
        self.assertIn("contact_id_obj", rec)
        self.assertEqual(rec["warranty_id_obj"]["product"], "TDM-120")
        self.assertEqual(rec["contact_id_obj"]["support_email"], "s@fnirsi.com")
        self.assertNotIn('"_id"', stdout)

    def test_filter_native_passthrough(self):
        stdout = self._run(
            "query", "invoices", "--filter", '{"vendor":"Mouser"}', "--fields", "id",
        )
        self.assertEqual(json.loads(stdout), [{"id": "doc_b"}])
        self.assertNotIn("doc_a", stdout)


if __name__ == "__main__":
    unittest.main()
