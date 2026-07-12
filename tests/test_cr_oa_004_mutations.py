"""CR-OA-004 Cycle A — `set-status` / `action-add` / `action-resolve` / `doc-add` on pymongo.

Verifies the §S1/§S2/§S3 ACs for porting the four tracking-mutation verbs off JSONL
`load()`/`save()` onto Mongo (`oa_mongo.coll(t)`, database honouring `OA_MONGO_DB`):

  - `set-status <type> <STATUS> --id <id>` `$set`s status + `updated` on ONE Mongo doc
    and returns {"status":..., "count":1, "ids":[id]}.
  - `set-status <type> <STATUS> --where f=v` `$set`s status + `updated` on every
    matching Mongo doc (bulk) and returns the matched count/ids.
  - `action-add <type> <id> <slug> [--detail T]` `$push`es an OPEN action (with an
    `opened` date) onto the Mongo doc's `actions[]`.
  - `action-resolve <type> <id> <slug>` flips a matching OPEN action to RESOLVED with
    a `resolved` date, in Mongo.
  - `doc-add <type> <id> <asset-type> <path>` `$push`es a document descriptor onto the
    Mongo doc's `documents[]`.

Currently all four verbs still call `load()`/`save()` on the real `data/*.jsonl` files
(only `query`/`get` were ported in CR-OA-003 Cycle A, and `add`/`update`/`rm`/`stats`
in CR-OA-003 Cycle B), so every test here MUST fail against a seeded
`office_assistant_test` Mongo database until CR-OA-004 Cycle A's GREEN phase ports
these four commands too.

DATA SAFETY: every subprocess call points `OA_DATA_DIR` at an EMPTY tempdir (never the
real repo `data/`), so the current JSONL-backed verbs read/write nothing but that
tempdir. No real `data/*.jsonl` file is ever opened by this test.
Requires a local mongod on 127.0.0.1:27017 (the office_assistant instance; CR-OA-001).
"""
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
sys.path.insert(0, SCRIPTS)


class TrackingVerbsMongoTest(unittest.TestCase):
    """§S1/§S2/§S3 — set-status/action-add/action-resolve/doc-add mutate Mongo, not JSONL."""

    TEST_DB = "office_assistant_test"
    COLLECTIONS = ["contacts", "invoices", "warranties", "cases", "products"]

    def setUp(self):
        # An EMPTY data dir: the current JSONL-backed verbs must find nothing here,
        # proving they are NOT silently reading/writing the real data/*.jsonl files.
        self.data_dir = tempfile.mkdtemp(prefix="oa-empty-data-")

        self.env = dict(os.environ)
        self.env["OA_MONGO_DB"] = self.TEST_DB
        self.env["OA_DATA_DIR"] = self.data_dir
        self.env["OA_FORMAT"] = "json"

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

    # ---- §S1 set-status --id (single) ----

    def test_set_status_by_id_updates_single_mongo_doc(self):
        self.db["invoices"].insert_one({"id": "doc_s", "status": "NEW"})

        result = self._run("set-status", "invoices", "COMPLETED", "--id", "doc_s")

        self.assertEqual(
            json.loads(result.stdout.strip()),
            {"status": "COMPLETED", "count": 1, "ids": ["doc_s"]},
        )
        doc = self.db["invoices"].find_one({"id": "doc_s"})
        self.assertIsNotNone(doc, "doc_s must still exist in the Mongo test DB")
        self.assertEqual(doc["status"], "COMPLETED")
        self.assertEqual(doc["updated"], date.today().isoformat())

    # ---- §S1 set-status --where (bulk) ----

    def test_set_status_by_where_updates_all_matching_mongo_docs(self):
        self.db["invoices"].insert_many([
            {"id": "doc_w1", "vendor": "Acme", "status": "NEW"},
            {"id": "doc_w2", "vendor": "Acme", "status": "NEW"},
            {"id": "doc_w3", "vendor": "OtherCo", "status": "NEW"},
        ])

        result = self._run("set-status", "invoices", "COMPLETED", "--where", "vendor=Acme")

        payload = json.loads(result.stdout.strip())
        self.assertEqual(payload["status"], "COMPLETED")
        self.assertEqual(payload["count"], 2)
        self.assertEqual(sorted(payload["ids"]), ["doc_w1", "doc_w2"])
        doc1 = self.db["invoices"].find_one({"id": "doc_w1"})
        doc2 = self.db["invoices"].find_one({"id": "doc_w2"})
        self.assertEqual(doc1["status"], "COMPLETED")
        self.assertEqual(doc2["status"], "COMPLETED")
        # negative bound: the non-matching vendor's doc is untouched
        doc3 = self.db["invoices"].find_one({"id": "doc_w3"})
        self.assertEqual(doc3["status"], "NEW")

    # ---- §S2 action-add ----

    def test_action_add_pushes_open_action_onto_mongo_doc(self):
        self.db["warranties"].insert_one({"id": "war_a"})

        result = self._run(
            "action-add", "warranties", "war_a", "capture-serial", "--detail", "get the serial",
        )

        self.assertEqual(result.stdout.strip() and json.loads(result.stdout.strip())["id"], "war_a")
        doc = self.db["warranties"].find_one({"id": "war_a"})
        self.assertIsNotNone(doc)
        actions = doc.get("actions", [])
        # negative bound: exactly one action landed, not accumulating stray pushes
        self.assertEqual(len(actions), 1)
        self.assertEqual(
            actions[-1],
            {"action": "capture-serial", "status": "OPEN", "opened": date.today().isoformat(),
             "detail": "get the serial"},
        )

    # ---- §S2 action-resolve ----

    def test_action_resolve_flips_matching_open_action_to_resolved(self):
        self.db["warranties"].insert_one({
            "id": "war_b",
            "actions": [{"action": "capture-serial", "status": "OPEN", "opened": "2026-07-01"}],
        })

        result = self._run("action-resolve", "warranties", "war_b", "capture-serial")

        self.assertEqual(result.stdout.strip() and json.loads(result.stdout.strip())["id"], "war_b")
        doc = self.db["warranties"].find_one({"id": "war_b"})
        self.assertIsNotNone(doc)
        actions = doc.get("actions", [])
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["status"], "RESOLVED")
        self.assertEqual(actions[0]["resolved"], date.today().isoformat())
        # negative bound: the original OPEN/opened data is preserved, not clobbered
        self.assertEqual(actions[0]["action"], "capture-serial")
        self.assertEqual(actions[0]["opened"], "2026-07-01")

    def test_action_resolve_on_already_resolved_action_errors_and_leaves_it_alone(self):
        self.db["warranties"].insert_one({
            "id": "war_c",
            "actions": [{"action": "capture-serial", "status": "RESOLVED",
                         "opened": "2026-07-01", "resolved": "2026-07-02"}],
        })

        result = self._run(
            "action-resolve", "warranties", "war_c", "capture-serial", expect_success=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            json.loads(result.stdout.strip()),
            {"error": "no OPEN action", "id": "war_c", "action": "capture-serial"},
        )
        # negative bound: the already-resolved action is untouched (no double-resolve)
        doc = self.db["warranties"].find_one({"id": "war_c"})
        self.assertEqual(doc["actions"][0]["resolved"], "2026-07-02")

    # ---- §S3 doc-add ----

    def test_doc_add_pushes_document_onto_mongo_doc(self):
        self.db["invoices"].insert_one({"id": "doc_d"})

        result = self._run(
            "doc-add", "invoices", "doc_d", "invoice", "documents/personal/x.pdf",
        )

        self.assertEqual(result.stdout.strip() and json.loads(result.stdout.strip())["id"], "doc_d")
        doc = self.db["invoices"].find_one({"id": "doc_d"})
        self.assertIsNotNone(doc)
        documents = doc.get("documents", [])
        # negative bound: exactly one document entry, not accumulating stray pushes
        self.assertEqual(len(documents), 1)
        self.assertEqual(
            documents[-1],
            {"type": "invoice", "path": "documents/personal/x.pdf"},
        )


if __name__ == "__main__":
    unittest.main()
