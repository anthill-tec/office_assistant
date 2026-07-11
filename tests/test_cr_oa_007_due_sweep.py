"""CR-OA-007 Cycle B (§S4) — `due-sweep` verb for the recurring stores.

Verifies: given a `subscriptions` doc whose `renews` date falls within the default
30-day lookahead of today and whose `status != "DUE"`, `store.py due-sweep` emits the
`renewal-window` transition through the shared `_apply_transition` engine
(`transitions.find_transition("subscriptions", "IN_PROGRESS", "renewal-window")`) —
setting `status=="DUE"` and appending an OPEN `cancel-before-charge` action (per
`transitions.TRANSITIONS["subscriptions"]`) — while a far-future doc is left alone. A
second sweep is idempotent (the `status != "DUE"` filter skips it: no duplicate
action). `--dry-run` performs the same lookahead query but writes nothing to Mongo.

`due-sweep` does not exist yet as a subparser on `scripts/store.py` (only `query`,
`get`, `add`, `update`, `rm`, `stats`, `set-status`, `action-add`, `action-resolve`,
`doc-add`, `attention`, `warranty-sweep`, `event`, `validate`, `import`, `snapshot`,
`init`, `apply-validators` exist), so EVERY test here MUST fail against the current
implementation: argparse rejects the `due-sweep` command (rc != 0, "invalid choice"),
so no test can reach the point of asserting the post-sweep Mongo state.

DATA SAFETY: every subprocess call points `OA_DATA_DIR` at a fresh EMPTY tempdir
(never the real repo `data/`) and `OA_MONGO_DB` at `office_assistant_test` (never the
real DB). Requires a local mongod on 127.0.0.1:27017 (CR-OA-001).
"""
import datetime
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


def _iso(days_from_today):
    return (datetime.date.today() + datetime.timedelta(days=days_from_today)).isoformat()


class DueSweepEngineTest(unittest.TestCase):
    """§S4 — `due-sweep` drives the recurring stores through the transition engine."""

    TEST_DB = "office_assistant_test"

    def setUp(self):
        # An EMPTY data dir: due-sweep must operate on Mongo, never the real
        # data/subscriptions.jsonl file.
        self.data_dir = tempfile.mkdtemp(prefix="oa-empty-data-")

        self.env = dict(os.environ)
        self.env["OA_MONGO_DB"] = self.TEST_DB
        self.env["OA_DATA_DIR"] = self.data_dir

        self.client = pymongo.MongoClient("mongodb://127.0.0.1:27017", serverSelectionTimeoutMS=2000)
        self.db = self.client[self.TEST_DB]
        self.db["subscriptions"].create_index("id", unique=True)

    def tearDown(self):
        self.client.drop_database(self.TEST_DB)
        self.client.close()
        shutil.rmtree(self.data_dir, ignore_errors=True)

    def _sweep(self, dry_run=False):
        cmd = [sys.executable, STORE, "due-sweep"]
        if dry_run:
            cmd.append("--dry-run")
        result = subprocess.run(cmd, capture_output=True, text=True, env=self.env)
        self.assertEqual(
            result.returncode, 0,
            f"due-sweep failed (rc={result.returncode}): {result.stderr}",
        )
        return result

    @staticmethod
    def _cancel_actions(doc):
        return [act for act in doc.get("actions", []) if act.get("action") == "cancel-before-charge"]

    def test_sweep_marks_within_window_subscription_due_and_leaves_far_future_one_alone(self):
        self.db["subscriptions"].insert_one(
            {"id": "sub_due", "status": "IN_PROGRESS", "renews": _iso(10)}
        )
        self.db["subscriptions"].insert_one(
            {"id": "sub_far", "status": "IN_PROGRESS", "renews": _iso(300)}
        )

        self._sweep()

        due = self.db["subscriptions"].find_one({"id": "sub_due"}, {"_id": 0})
        self.assertIsNotNone(due, "sub_due must still exist in the Mongo test DB")
        self.assertEqual(due["status"], "DUE")
        open_cancel = [act for act in self._cancel_actions(due) if act.get("status") == "OPEN"]
        self.assertEqual(
            len(open_cancel), 1,
            f"expected exactly one OPEN cancel-before-charge action on sub_due, got {due.get('actions', [])}",
        )

        far = self.db["subscriptions"].find_one({"id": "sub_far"}, {"_id": 0})
        self.assertIsNotNone(far, "sub_far must still exist in the Mongo test DB")
        # negative bound: the far-future subscription (outside the lookahead) is left completely alone
        self.assertEqual(far["status"], "IN_PROGRESS")
        self.assertEqual(
            self._cancel_actions(far), [],
            "sub_far (outside the lookahead) must not gain a cancel-before-charge action",
        )

    def test_sweep_is_idempotent_no_duplicate_cancel_action_on_second_run(self):
        self.db["subscriptions"].insert_one(
            {"id": "sub_due", "status": "IN_PROGRESS", "renews": _iso(10)}
        )

        self._sweep()
        self._sweep()

        doc = self.db["subscriptions"].find_one({"id": "sub_due"}, {"_id": 0})
        self.assertIsNotNone(doc, "sub_due must still exist in the Mongo test DB")
        self.assertEqual(doc["status"], "DUE")
        open_cancel = [act for act in self._cancel_actions(doc) if act.get("status") == "OPEN"]
        self.assertEqual(
            len(open_cancel), 1,
            f"expected exactly one OPEN cancel-before-charge action after two sweeps, got {doc.get('actions', [])}",
        )
        # negative bound: no stray actions of any kind were pushed alongside it
        self.assertEqual(len(doc.get("actions", [])), 1)

    def test_dry_run_makes_no_write_but_exits_cleanly(self):
        self.db["subscriptions"].insert_one(
            {"id": "sub_dry", "status": "IN_PROGRESS", "renews": _iso(5)}
        )

        self._sweep(dry_run=True)

        doc = self.db["subscriptions"].find_one({"id": "sub_dry"}, {"_id": 0})
        self.assertIsNotNone(doc, "sub_dry must still exist in the Mongo test DB")
        # positive/negative bound: --dry-run must not flip status or push any action
        self.assertEqual(doc["status"], "IN_PROGRESS")
        self.assertEqual(
            self._cancel_actions(doc), [],
            "--dry-run must not push a cancel-before-charge action",
        )
        self.assertEqual(doc.get("actions", []), [], "--dry-run must not mutate actions at all")


class InsuranceDueSweepTest(unittest.TestCase):
    """§S4 — `due-sweep` must ALSO sweep `insurance` docs, which carry an `expiry`
    date (not `renews`). Today `cmd_due_sweep` (scripts/store.py) queries every
    recurring store — including `insurance`, since `transitions.TRANSITIONS`
    declares an `IN_PROGRESS --renewal-window--> DUE` transition for it — with
    `coll.find({"renews": {"$lte": cutoff}, ...})` only. An `insurance` doc has no
    `renews` field, so that query NEVER matches it: the doc stays `IN_PROGRESS`
    forever, no matter how close its `expiry` is. This test seeds an `insurance`
    doc via its `expiry` field and proves the sweep must promote it to `DUE` and
    open the `renew-policy` action (transitions.TRANSITIONS["insurance"][0]:
    `{"from": "IN_PROGRESS", "event": "renewal-window", "to": "DUE",
    "effects": [{"op": "open-action", "action": "renew-policy", ...}]}`) via the
    shared `_apply_transition` engine — the same engine subscriptions already use."""

    TEST_DB = "office_assistant_test"

    def setUp(self):
        # An EMPTY data dir: due-sweep must operate on Mongo, never the real
        # data/insurance.jsonl file.
        self.data_dir = tempfile.mkdtemp(prefix="oa-empty-data-")

        self.env = dict(os.environ)
        self.env["OA_MONGO_DB"] = self.TEST_DB
        self.env["OA_DATA_DIR"] = self.data_dir

        self.client = pymongo.MongoClient("mongodb://127.0.0.1:27017", serverSelectionTimeoutMS=2000)
        self.db = self.client[self.TEST_DB]
        self.db["insurance"].create_index("id", unique=True)

    def tearDown(self):
        self.client.drop_database(self.TEST_DB)
        self.client.close()
        shutil.rmtree(self.data_dir, ignore_errors=True)

    def _sweep(self, dry_run=False):
        cmd = [sys.executable, STORE, "due-sweep"]
        if dry_run:
            cmd.append("--dry-run")
        result = subprocess.run(cmd, capture_output=True, text=True, env=self.env)
        self.assertEqual(
            result.returncode, 0,
            f"due-sweep failed (rc={result.returncode}): {result.stderr}",
        )
        return result

    @staticmethod
    def _renew_actions(doc):
        return [act for act in doc.get("actions", []) if act.get("action") == "renew-policy"]

    def test_sweep_marks_within_window_insurance_due_by_expiry_and_leaves_far_future_one_alone(self):
        self.db["insurance"].insert_one(
            {"id": "ins_test_within", "status": "IN_PROGRESS", "expiry": _iso(10), "product_id": "prod_x"}
        )
        self.db["insurance"].insert_one(
            {"id": "ins_test_far", "status": "IN_PROGRESS", "expiry": _iso(300), "product_id": "prod_x"}
        )

        self._sweep()

        within = self.db["insurance"].find_one({"id": "ins_test_within"}, {"_id": 0})
        self.assertIsNotNone(within, "ins_test_within must still exist in the Mongo test DB")
        self.assertEqual(
            within["status"], "DUE",
            "insurance doc within the 30-day lookahead (by expiry) must be swept to DUE",
        )
        open_renew = [act for act in self._renew_actions(within) if act.get("status") == "OPEN"]
        self.assertEqual(
            len(open_renew), 1,
            f"expected exactly one OPEN renew-policy action on ins_test_within, got {within.get('actions', [])}",
        )

        far = self.db["insurance"].find_one({"id": "ins_test_far"}, {"_id": 0})
        self.assertIsNotNone(far, "ins_test_far must still exist in the Mongo test DB")
        # negative bound: the far-future policy (outside the lookahead) is left completely alone
        self.assertEqual(far["status"], "IN_PROGRESS")
        self.assertEqual(
            self._renew_actions(far), [],
            "ins_test_far (outside the lookahead) must not gain a renew-policy action",
        )

    def test_sweep_is_idempotent_no_duplicate_renew_action_on_second_run(self):
        self.db["insurance"].insert_one(
            {"id": "ins_test_within", "status": "IN_PROGRESS", "expiry": _iso(10), "product_id": "prod_x"}
        )

        self._sweep()
        self._sweep()

        doc = self.db["insurance"].find_one({"id": "ins_test_within"}, {"_id": 0})
        self.assertIsNotNone(doc, "ins_test_within must still exist in the Mongo test DB")
        self.assertEqual(doc["status"], "DUE")
        open_renew = [act for act in self._renew_actions(doc) if act.get("status") == "OPEN"]
        self.assertEqual(
            len(open_renew), 1,
            f"expected exactly one OPEN renew-policy action after two sweeps, got {doc.get('actions', [])}",
        )
        # negative bound: no stray actions of any kind were pushed alongside it
        self.assertEqual(len(doc.get("actions", [])), 1)


if __name__ == "__main__":
    unittest.main()
