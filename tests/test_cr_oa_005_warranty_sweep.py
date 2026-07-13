"""CR-OA-005 Cycle B — `warranty-sweep` re-expressed through the transition engine.

Verifies the §S3 AC: given a warranty whose `expiry` is before today and whose
`status != "EXPIRED"`, `store.py warranty-sweep` emits the `expire` transition
against the Mongo doc (via `transitions.find_transition`, applying its effects) —
setting `status=="EXPIRED"` and appending an OPEN `renew-or-extend` action — and a
second sweep is idempotent (no duplicate action, no re-processing).

Cycle A's `store.py event` already drives Mongo docs through the transition table,
but `cmd_warranty_sweep` (scripts/store.py) still reads/writes the JSONL store via
`load()`/`save()` and never touches Mongo at all. So EVERY test here MUST fail
against the current implementation: warranties seeded directly into
`vidushi_oa_test.warranties` are never read by `warranty-sweep` (it reads the
real on-disk `data/warranties.jsonl` instead), so the seeded Mongo docs stay
untouched and every assertion below on their post-sweep state fails.

DATA SAFETY: every subprocess call points `VIDUSHI_DATA_DIR` at a fresh EMPTY tempdir
(never the real repo `data/`) and `VIDUSHI_MONGO_DB` at `vidushi_oa_test` (never
the real DB). Requires a local mongod on 127.0.0.1:27017 (CR-OA-001).
"""
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


class WarrantySweepEngineTest(unittest.TestCase):
    """§S3 — `warranty-sweep` re-expressed through the transition engine, on Mongo."""

    TEST_DB = "vidushi_oa_test"

    def setUp(self):
        # An EMPTY data dir: proves warranty-sweep is not silently reading/writing
        # the real data/warranties.jsonl file.
        self.data_dir = tempfile.mkdtemp(prefix="oa-empty-data-")

        self.env = dict(os.environ)
        self.env["VIDUSHI_MONGO_DB"] = self.TEST_DB
        self.env["VIDUSHI_DATA_DIR"] = self.data_dir

        self.client = pymongo.MongoClient("mongodb://127.0.0.1:27017", serverSelectionTimeoutMS=2000)
        self.db = self.client[self.TEST_DB]
        self.db["warranties"].create_index("id", unique=True)

    def tearDown(self):
        self.client.drop_database(self.TEST_DB)
        self.client.close()
        shutil.rmtree(self.data_dir, ignore_errors=True)

    def _sweep(self):
        result = subprocess.run(
            [sys.executable, STORE, "warranty-sweep"],
            capture_output=True, text=True, env=self.env,
        )
        self.assertEqual(
            result.returncode, 0,
            f"warranty-sweep failed (rc={result.returncode}): {result.stderr}",
        )
        return result

    @staticmethod
    def _renew_actions(doc):
        return [act for act in doc.get("actions", []) if act.get("action") == "renew-or-extend"]

    def test_sweep_expires_past_due_warranty_via_engine_and_leaves_future_one_alone(self):
        self.db["warranties"].insert_one(
            {"id": "war_exp", "status": "IN_PROGRESS", "expiry": "2020-01-01"}
        )
        self.db["warranties"].insert_one(
            {"id": "war_active", "status": "IN_PROGRESS", "expiry": "2099-01-01"}
        )

        self._sweep()

        expired = self.db["warranties"].find_one({"id": "war_exp"}, {"_id": 0})
        self.assertIsNotNone(expired, "war_exp must still exist in the Mongo test DB")
        self.assertEqual(expired["status"], "EXPIRED")
        open_renew = [act for act in self._renew_actions(expired) if act.get("status") == "OPEN"]
        self.assertEqual(
            len(open_renew), 1,
            f"expected exactly one OPEN renew-or-extend action on war_exp, got {expired.get('actions', [])}",
        )

        active = self.db["warranties"].find_one({"id": "war_active"}, {"_id": 0})
        self.assertIsNotNone(active, "war_active must still exist in the Mongo test DB")
        # negative bound: the not-yet-expired warranty is left completely alone
        self.assertEqual(active["status"], "IN_PROGRESS")
        self.assertEqual(
            self._renew_actions(active), [],
            "war_active (not yet expired) must not gain a renew-or-extend action",
        )

    def test_sweep_is_idempotent_no_duplicate_renew_action_on_second_run(self):
        self.db["warranties"].insert_one(
            {"id": "war_exp", "status": "IN_PROGRESS", "expiry": "2020-01-01"}
        )

        self._sweep()
        self._sweep()

        doc = self.db["warranties"].find_one({"id": "war_exp"}, {"_id": 0})
        self.assertIsNotNone(doc, "war_exp must still exist in the Mongo test DB")
        self.assertEqual(doc["status"], "EXPIRED")
        open_renew = [act for act in self._renew_actions(doc) if act.get("status") == "OPEN"]
        self.assertEqual(
            len(open_renew), 1,
            f"expected exactly one OPEN renew-or-extend action after two sweeps, got {doc.get('actions', [])}",
        )
        # negative bound: no stray actions of any kind were pushed alongside it
        self.assertEqual(len(doc.get("actions", [])), 1)


if __name__ == "__main__":
    unittest.main()
