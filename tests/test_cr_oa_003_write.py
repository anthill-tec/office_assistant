"""CR-OA-003 Cycle B — `store.py` write-path (`add`/`update`/`rm`/`stats`) on pymongo.

Verifies the §S3 ACs for the write-side backend swap:
  - `add` persists a new document to Mongo and returns {"added":[...],"skipped":[]}
  - `add` on an existing id is deduped via the unique `id` index -> {"added":[],"skipped":[id]}
  - `add` accepts a bulk array and persists every record
  - `add` without an `id` generates one via `gen_id` (vendor slug prefix) and persists it
  - `update` shallow-merges the patch (`$set`) and bumps `updated`
  - `update --append-log` pushes a {date, note} entry onto `log`
  - `rm` removes the document and reports the remaining count
  - `stats` / `stats --by field` count Mongo documents (not the JSONL file)

Currently `add`/`update`/`rm`/`stats` still call `load()`/`save()` on the real
`data/*.jsonl` files (Cycle A only ported `query`/`get`), so every test here MUST fail
against a seeded `office_assistant_test` Mongo database until Cycle B's GREEN phase
ports these four commands too.

DATA SAFETY: the current write commands operate on the REAL `data/*.jsonl` files, so
this test backs up every `data/*.jsonl` in `setUp` and restores it verbatim in
`tearDown` (regardless of pass/fail), on top of dropping the Mongo test database.
Requires a local mongod on 127.0.0.1:27017 (same instance CR-OA-001/002 pin to).
"""
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date

import pymongo

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, "scripts")
STORE = os.path.join(SCRIPTS, "store.py")
DATA = os.path.join(ROOT, "data")
sys.path.insert(0, SCRIPTS)


class WritePathMongoTest(unittest.TestCase):
    """§S3 — add/update/rm/stats write to Mongo (office_assistant_test), preserving CLI semantics."""

    TEST_DB = "office_assistant_test"
    COLLECTIONS = ["contacts", "invoices", "warranties", "cases", "products"]

    def setUp(self):
        # --- data safety: snapshot every real data/*.jsonl before store.py can touch it ---
        self.backup_dir = tempfile.mkdtemp(prefix="oa-jsonl-backup-")
        self.real_jsonl = glob.glob(os.path.join(DATA, "*.jsonl"))
        for f in self.real_jsonl:
            shutil.copy(f, os.path.join(self.backup_dir, os.path.basename(f)))

        self.env = dict(os.environ)
        self.env["OA_MONGO_DB"] = self.TEST_DB

        self.client = pymongo.MongoClient("mongodb://127.0.0.1:27017", serverSelectionTimeoutMS=2000)
        self.db = self.client[self.TEST_DB]
        # Mirror `store.py init`'s unique `id` index so post-GREEN DuplicateKeyError-based
        # dedupe has something to trip over.
        for c in self.COLLECTIONS:
            self.db[c].create_index("id", unique=True)

    def tearDown(self):
        # Restore the real data files exactly as they were, no matter what happened above.
        for f in self.real_jsonl:
            shutil.copy(os.path.join(self.backup_dir, os.path.basename(f)), f)
        shutil.rmtree(self.backup_dir, ignore_errors=True)

        self.client.drop_database(self.TEST_DB)
        self.client.close()

    def _run(self, *args, expect_success=True):
        result = subprocess.run(
            [sys.executable, STORE, *args], capture_output=True, text=True, env=self.env,
        )
        if expect_success:
            self.assertEqual(
                result.returncode, 0,
                f"store.py {' '.join(args)} failed (rc={result.returncode}): {result.stderr}",
            )
        return result

    # ---- add ----

    def test_add_new_document_persists_to_mongo(self):
        result = self._run(
            "add", "invoices", "--json",
            json.dumps({"id": "doc_new", "vendor": "X", "doc_type": "invoice",
                        "date": "2026-01-01", "acct": "personal", "status": "NEW"}),
        )
        self.assertEqual(json.loads(result.stdout.strip()), {"added": ["doc_new"], "skipped": []})
        doc = self.db["invoices"].find_one({"id": "doc_new"})
        self.assertIsNotNone(doc, "doc_new must be persisted to the Mongo test DB, not JSONL")
        self.assertEqual(doc["vendor"], "X")

    def test_add_existing_id_is_deduped_via_unique_index(self):
        self.db["invoices"].insert_one({"id": "doc_dup", "vendor": "Y", "status": "NEW"})
        result = self._run(
            "add", "invoices", "--json",
            json.dumps({"id": "doc_dup", "vendor": "Y", "status": "NEW"}),
        )
        self.assertEqual(json.loads(result.stdout.strip()), {"added": [], "skipped": ["doc_dup"]})
        # negative bound: exactly one doc_dup remains -- no duplicate got through
        self.assertEqual(self.db["invoices"].count_documents({"id": "doc_dup"}), 1)

    def test_add_bulk_array_adds_all_and_persists_to_mongo(self):
        result = self._run(
            "add", "warranties", "--json",
            json.dumps([
                {"id": "war_1", "product": "P1", "expiry": "2027-01-01"},
                {"id": "war_2", "product": "P2", "expiry": "2027-06-01"},
            ]),
        )
        payload = json.loads(result.stdout.strip())
        self.assertEqual(sorted(payload["added"]), ["war_1", "war_2"])
        self.assertEqual(payload["skipped"], [])
        # negative bound: exactly the two seeded records, nothing extra
        self.assertEqual(self.db["warranties"].count_documents({}), 2)
        self.assertIsNotNone(self.db["warranties"].find_one({"id": "war_1"}))
        self.assertIsNotNone(self.db["warranties"].find_one({"id": "war_2"}))

    def test_add_without_id_generates_vendor_slug_id_in_mongo(self):
        result = self._run("add", "contacts", "--json", json.dumps({"vendor": "AcmeCo"}))
        payload = json.loads(result.stdout.strip())
        self.assertEqual(len(payload["added"]), 1)
        gen_id = payload["added"][0]
        self.assertTrue(gen_id.startswith("ven_acmeco"), gen_id)
        self.assertEqual(payload["skipped"], [])
        doc = self.db["contacts"].find_one({"id": gen_id})
        self.assertIsNotNone(doc, "generated-id contact must be persisted to Mongo")
        self.assertEqual(doc["vendor"], "AcmeCo")

    # ---- update ----

    def test_update_sets_patch_fields_and_bumps_updated(self):
        self.db["invoices"].insert_one({"id": "doc_u", "status": "NEW"})
        result = self._run("update", "invoices", "doc_u", "--json", json.dumps({"status": "COMPLETED"}))
        self.assertEqual(json.loads(result.stdout.strip()), {"updated": "doc_u"})
        doc = self.db["invoices"].find_one({"id": "doc_u"})
        self.assertEqual(doc["status"], "COMPLETED")
        self.assertEqual(doc["updated"], date.today().isoformat())

    def test_update_append_log_pushes_note_with_date(self):
        self.db["cases"].insert_one({"id": "case_x"})
        result = self._run("update", "cases", "case_x", "--append-log", "hello")
        self.assertEqual(json.loads(result.stdout.strip()), {"updated": "case_x"})
        doc = self.db["cases"].find_one({"id": "case_x"})
        # negative bound: exactly one log entry, not accumulating stray pushes
        self.assertEqual(len(doc.get("log", [])), 1)
        self.assertEqual(doc["log"][-1]["note"], "hello")
        self.assertIn("date", doc["log"][-1])

    # ---- rm ----

    def test_rm_removes_document_and_reports_remaining_count(self):
        self.db["invoices"].insert_many([
            {"id": "doc_r", "vendor": "Z"},
            {"id": "doc_other1", "vendor": "Z"},
            {"id": "doc_other2", "vendor": "Z"},
        ])
        result = self._run("rm", "invoices", "doc_r")
        self.assertEqual(json.loads(result.stdout.strip()), {"removed": "doc_r", "remaining": 2})
        self.assertIsNone(self.db["invoices"].find_one({"id": "doc_r"}))
        # negative bound: the other two survive, remaining count matches exactly
        self.assertEqual(self.db["invoices"].count_documents({}), 2)

    # ---- stats ----

    def test_stats_total_counts_mongo_documents(self):
        self.db["invoices"].insert_many([
            {"id": "doc_s1", "status": "COMPLETED"},
            {"id": "doc_s2", "status": "COMPLETED"},
            {"id": "doc_s3", "status": "NEW"},
        ])
        result = self._run("stats", "invoices")
        self.assertEqual(json.loads(result.stdout.strip()), {"type": "invoices", "total": 3})

    def test_stats_by_field_groups_via_mongo(self):
        self.db["invoices"].insert_many([
            {"id": "doc_s1", "status": "COMPLETED"},
            {"id": "doc_s2", "status": "COMPLETED"},
            {"id": "doc_s3", "status": "NEW"},
        ])
        result = self._run("stats", "invoices", "--by", "status")
        payload = json.loads(result.stdout.strip())
        self.assertEqual(payload["type"], "invoices")
        self.assertEqual(payload["total"], 3)
        self.assertEqual(payload["by"], "status")
        # exact counts -- both the COMPLETED and NEW buckets, nothing else
        self.assertEqual(payload["counts"], {"COMPLETED": 2, "NEW": 1})


if __name__ == "__main__":
    unittest.main()
