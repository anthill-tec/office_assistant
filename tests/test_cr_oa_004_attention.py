"""CR-OA-004 Cycle B — `attention` on pymongo.

Verifies the §S4 AC for porting `attention` off JSONL `load()` onto Mongo
(`oa_mongo.coll(t)`, database honouring `OA_MONGO_DB`):

  - `attention [<type>]` returns records with an OPEN action OR an explicit
    `status in {NEW, UNKNOWN, EXPIRED, DUE}`; a record with NO `status` field at
    all is NOT flagged. Projects {type, id, name, status, open_actions}.

Currently `attention` still calls `load()` on the real `data/*.jsonl` files, so
this test MUST fail against a seeded `office_assistant_test` Mongo database until
CR-OA-004 Cycle B's GREEN phase ports this command too.

DATA SAFETY: every subprocess call points `OA_DATA_DIR` at an EMPTY tempdir (never the
real repo `data/`), so the current JSONL-backed verb reads/writes nothing but that
tempdir. No real `data/*.jsonl` file is ever opened by this test.
Requires a local mongod on 127.0.0.1:27017 (the office_assistant instance; CR-OA-001).

NOTE: `warranty-sweep` is OUT OF SCOPE for CR-OA-004 (belongs to CR-OA-005's
transition engine) and is intentionally NOT tested here.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import pymongo

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, "scripts")
STORE = os.path.join(SCRIPTS, "store.py")
sys.path.insert(0, SCRIPTS)


class AttentionMongoTest(unittest.TestCase):
    """§S4 `attention` reads/projects from Mongo, not JSONL."""

    TEST_DB = "office_assistant_test"
    COLLECTIONS = ["contacts", "invoices", "warranties", "cases", "products"]

    def setUp(self):
        # An EMPTY data dir: the current JSONL-backed verb must find nothing here,
        # proving it is NOT silently reading/writing the real data/*.jsonl files.
        self.data_dir = tempfile.mkdtemp(prefix="oa-empty-data-")

        self.env = dict(os.environ)
        self.env["OA_MONGO_DB"] = self.TEST_DB
        self.env["OA_DATA_DIR"] = self.data_dir

        self.client = pymongo.MongoClient("mongodb://127.0.0.1:27017", serverSelectionTimeoutMS=2000)
        self.db = self.client[self.TEST_DB]
        for c in self.COLLECTIONS:
            self.db[c].create_index("id", unique=True)

    def tearDown(self):
        self.client.drop_database(self.TEST_DB)
        self.client.close()
        shutil.rmtree(self.data_dir, ignore_errors=True)

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

    # ---- §S4 attention ----

    def test_attention_flags_open_action_and_explicit_new_status_from_mongo(self):
        self.db["warranties"].insert_one({
            "id": "war_open", "status": "IN_PROGRESS",
            "actions": [{"action": "capture-serial", "status": "OPEN", "opened": "2026-07-01"}],
        })
        self.db["invoices"].insert_one({"id": "doc_done", "status": "COMPLETED"})
        self.db["invoices"].insert_one({"id": "doc_new", "status": "NEW"})
        self.db["contacts"].insert_one({"id": "ven_x"})  # no status field at all

        result = self._run("attention")

        payload = json.loads(result.stdout.strip())
        ids = {r["id"] for r in payload}
        # positive: exactly the two records that should surface, nothing more
        self.assertEqual(ids, {"war_open", "doc_new"})
        # negative bound: COMPLETED-with-no-open-action and status-less rows excluded
        self.assertNotIn("doc_done", ids)
        self.assertNotIn("ven_x", ids)

        war_entry = next(r for r in payload if r["id"] == "war_open")
        self.assertEqual(war_entry["open_actions"], ["capture-serial"])
        self.assertEqual(war_entry["type"], "warranties")
        self.assertEqual(war_entry["status"], "IN_PROGRESS")

        new_entry = next(r for r in payload if r["id"] == "doc_new")
        self.assertEqual(new_entry["status"], "NEW")
        self.assertEqual(new_entry["open_actions"], [])


if __name__ == "__main__":
    unittest.main()
