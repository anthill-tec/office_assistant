"""CR-OA-005 Cycle A — transition-map engine (`scripts/transitions.py`) + `event` verb.

Verifies the §S1/§S2 ACs for the declarative transition-table engine:

  - `transitions.TRANSITIONS[type]` is a list of `{from, event, to, owner, effects}`
    dicts. `TRANSITIONS["warranties"]` must contain the `IN_PROGRESS --expire--> EXPIRED`
    (owner "agent") transition whose effects open a `renew-or-extend` action for the
    user. `TRANSITIONS["invoices"]` must contain the `IN_PROGRESS --delivered-->
    COMPLETED` (owner "agent") transition.
  - `store.py event <type> <id> <event>` looks up the doc's current `status` in the
    table, applies the matching transition (setting `status` + firing `effects`) and
    rejects an event with no matching `(from, event)` pair, leaving the Mongo doc
    untouched.

Neither `scripts/transitions.py` nor the `event` verb exist yet (CR-OA-005 Cycle A is
still RED), so EVERY test here MUST fail: the table tests via ModuleNotFoundError on
`import transitions`, and the `event`-verb tests because `store.py` has no `event`
subparser (argparse rejects it) so nothing in Mongo is ever mutated.

DATA SAFETY: every subprocess call points `OA_DATA_DIR` at an EMPTY tempdir (never the
real repo `data/`) and `OA_MONGO_DB` at `office_assistant_test` (never the real DB).
Requires a local mongod on 127.0.0.1:27017 (the office_assistant instance; CR-OA-001).
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


class TransitionsTableTest(unittest.TestCase):
    """§S1 — `TRANSITIONS[type]` table shape (no Mongo needed; pure module import)."""

    def test_warranties_table_has_in_progress_expire_to_expired_with_renew_effect(self):
        sys.path.insert(0, SCRIPTS)
        import transitions

        table = transitions.TRANSITIONS["warranties"]
        matches = [t for t in table
                   if t.get("from") == "IN_PROGRESS" and t.get("event") == "expire"]
        self.assertEqual(
            len(matches), 1,
            f"expected exactly one IN_PROGRESS+expire transition in warranties, got {matches}",
        )
        entry = matches[0]
        self.assertEqual(entry.get("to"), "EXPIRED")
        self.assertEqual(entry.get("owner"), "agent")

        effects = entry.get("effects", [])
        renew_effects = [
            e for e in effects
            if e.get("op") == "open-action" and e.get("action") == "renew-or-extend"
        ]
        self.assertEqual(
            len(renew_effects), 1,
            f"expected exactly one open-action renew-or-extend effect, got {effects}",
        )
        self.assertEqual(renew_effects[0].get("owner"), "user")
        # negative bound: this transition is not also mislabeled as a different target
        self.assertFalse(
            any(t.get("event") == "expire" and t.get("to") != "EXPIRED" for t in table),
            "no other 'expire' transition in the warranties table may target a status other than EXPIRED",
        )

    def test_invoices_table_has_in_progress_delivered_to_completed(self):
        sys.path.insert(0, SCRIPTS)
        import transitions

        table = transitions.TRANSITIONS["invoices"]
        matches = [t for t in table
                   if t.get("from") == "IN_PROGRESS" and t.get("event") == "delivered"]
        self.assertEqual(
            len(matches), 1,
            f"expected exactly one IN_PROGRESS+delivered transition in invoices, got {matches}",
        )
        entry = matches[0]
        self.assertEqual(entry.get("to"), "COMPLETED")
        self.assertEqual(entry.get("owner"), "agent")
        # negative bound: 'delivered' from a status other than IN_PROGRESS is not in the table
        self.assertFalse(
            any(t.get("event") == "delivered" and t.get("from") != "IN_PROGRESS" for t in table),
            "no other 'delivered' transition in the invoices table may originate from a status "
            "other than IN_PROGRESS",
        )


class EventVerbTest(unittest.TestCase):
    """§S2 — `store.py event <type> <id> <event>` drives Mongo docs via the transition table."""

    TEST_DB = "office_assistant_test"
    COLLECTIONS = ["contacts", "invoices", "warranties", "cases", "products"]

    def setUp(self):
        # An EMPTY data dir: proves the event verb is not silently reading/writing
        # the real data/*.jsonl files.
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

    def test_event_delivered_advances_purchase_in_progress_to_completed(self):
        self.db["invoices"].insert_one({"id": "doc_e", "status": "IN_PROGRESS"})

        self._run("event", "invoices", "doc_e", "delivered")

        doc = self.db["invoices"].find_one({"id": "doc_e"})
        self.assertIsNotNone(doc, "doc_e must still exist in the Mongo test DB")
        self.assertEqual(doc["status"], "COMPLETED")
        # negative bound: the IN_PROGRESS->COMPLETED/delivered transition has no
        # effects of its own, so no actions[] should have been fabricated
        self.assertEqual(doc.get("actions", []), [])

    def test_event_delivered_on_completed_doc_is_rejected_and_leaves_doc_unchanged(self):
        self.db["invoices"].insert_one({"id": "doc_c", "status": "COMPLETED"})

        result = self._run("event", "invoices", "doc_c", "delivered", expect_success=False)

        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout.strip())
        self.assertIn("illegal transition", payload.get("error", ""))
        # negative bound: the doc's status is untouched by the rejected event
        doc = self.db["invoices"].find_one({"id": "doc_c"})
        self.assertIsNotNone(doc)
        self.assertEqual(doc["status"], "COMPLETED")

    def test_event_expire_fires_effects_and_opens_renew_or_extend_action(self):
        self.db["warranties"].insert_one({"id": "war_e", "status": "IN_PROGRESS"})

        self._run("event", "warranties", "war_e", "expire")

        doc = self.db["warranties"].find_one({"id": "war_e"})
        self.assertIsNotNone(doc, "war_e must still exist in the Mongo test DB")
        self.assertEqual(doc["status"], "EXPIRED")
        actions = doc.get("actions", [])
        open_renew = [
            act for act in actions
            if act.get("action") == "renew-or-extend" and act.get("status") == "OPEN"
        ]
        self.assertEqual(
            len(open_renew), 1,
            f"expected exactly one OPEN renew-or-extend action, got {actions}",
        )
        # negative bound: no other stray actions were pushed alongside it
        self.assertEqual(len(actions), 1)


if __name__ == "__main__":
    unittest.main()
