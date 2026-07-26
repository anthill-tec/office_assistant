"""CR-OA-015 Cycle A — §S2 the `orders` transition map.

Verifies the §S2 acceptance criteria for the fulfilment state machine, driven by the
existing CR-OA-005 declarative engine (`transitions.TRANSITIONS` + the `event` verb):

  - `TRANSITIONS["orders"]` maps the fulfilment vocabulary to transitions + effects,
    grounded in DN-purchases-persistence:
      * `shipped` / `out-for-delivery` ADVANCE the human-readable `stage` field while the
        coarse `status` stays `IN_PROGRESS` (a new `{"op": "set-stage"}` effect);
      * `delivered` lands `COMPLETED` (stage `Delivered`);
      * `held-at-customs` / `duty-demanded` / `kyc-requested` / `clarification-requested`
        open the matching OPEN action (`customs-clearance` / `duty-payment` / `kyc` /
        `clarification`) and hold `IN_PROGRESS`;
      * `cancelled` / `returned` / `refunded` / `delivery-failed` are terminal side-states —
        `status` `COMPLETED` with the flavour recorded in `stage`.
    An unmapped event is rejected with NO write (per the CR-OA-005 engine).
  - `store.py event orders <id> <event>` drives the Mongo doc through that table, and
    `store.py attention orders` surfaces an order that a customs event left with an OPEN
    action.

`TRANSITIONS["orders"]` does not exist yet and `_apply_transition` has no `set-stage` op
(CR-OA-015 §S2 is still RED), so EVERY test here MUST fail: the table tests via a
`KeyError` on `TRANSITIONS["orders"]`; the `event`-verb tests because `find_transition`
returns `None` for every orders event, so `event` prints `illegal transition` and exits
1 (the doc is never mutated) where the AC expects success + a status/stage change.

DATA SAFETY: every subprocess call points `VIDUSHI_DATA_DIR` at an EMPTY tempdir (never the
real repo `data/`) and `VIDUSHI_MONGO_DB` at `vidushi_oa_test` (never the real DB), dropped
in tearDown. Requires a local mongod on 127.0.0.1:27017 (the office_assistant instance;
CR-OA-001).
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

TEST_DB = "vidushi_oa_test"


class OrdersTransitionTableTest(unittest.TestCase):
    """§S2 — `TRANSITIONS["orders"]` table shape (pure module import, no Mongo)."""

    def _table(self):
        sys.path.insert(0, SCRIPTS)
        import transitions

        return transitions.TRANSITIONS["orders"]

    def _match(self, table, frm, event):
        return [t for t in table if t.get("from") == frm and t.get("event") == event]

    def test_in_progress_delivered_to_completed(self):
        table = self._table()
        matches = self._match(table, "IN_PROGRESS", "delivered")
        self.assertEqual(
            len(matches), 1,
            f"expected exactly one IN_PROGRESS+delivered transition in orders, got {matches}",
        )
        entry = matches[0]
        self.assertEqual(entry.get("to"), "COMPLETED")
        self.assertEqual(entry.get("owner"), "agent")
        # negative bound: 'delivered' never targets a status other than COMPLETED
        self.assertFalse(
            any(t.get("event") == "delivered" and t.get("to") != "COMPLETED" for t in table),
            "no 'delivered' transition in the orders table may target a status other than COMPLETED",
        )

    def test_shipped_and_out_for_delivery_advance_stage_within_in_progress(self):
        table = self._table()
        for event, stage in (("shipped", "Shipped"), ("out-for-delivery", "Out for delivery")):
            matches = self._match(table, "IN_PROGRESS", event)
            self.assertEqual(
                len(matches), 1,
                f"expected exactly one IN_PROGRESS+{event} transition in orders, got {matches}",
            )
            entry = matches[0]
            # a stage advance stays IN_PROGRESS — status must NOT flip to a terminal state
            self.assertEqual(entry.get("to"), "IN_PROGRESS")
            set_stage = [e for e in entry.get("effects", [])
                         if e.get("op") == "set-stage" and e.get("stage") == stage]
            self.assertEqual(
                len(set_stage), 1,
                f"expected exactly one set-stage->{stage} effect for {event}, got {entry.get('effects')}",
            )
            # negative bound: a pure stage advance opens no action
            self.assertFalse(
                any(e.get("op") == "open-action" for e in entry.get("effects", [])),
                f"{event} is a stage advance and must open no action",
            )

    def test_customs_events_open_their_matching_open_action(self):
        table = self._table()
        expected = {
            "held-at-customs": "customs-clearance",
            "duty-demanded": "duty-payment",
            "kyc-requested": "kyc",
            "clarification-requested": "clarification",
        }
        for event, action in expected.items():
            matches = self._match(table, "IN_PROGRESS", event)
            self.assertEqual(
                len(matches), 1,
                f"expected exactly one IN_PROGRESS+{event} transition in orders, got {matches}",
            )
            entry = matches[0]
            # customs sub-states hold IN_PROGRESS (the parcel is still in flight)
            self.assertEqual(entry.get("to"), "IN_PROGRESS")
            opens = [e for e in entry.get("effects", [])
                     if e.get("op") == "open-action" and e.get("action") == action]
            self.assertEqual(
                len(opens), 1,
                f"expected exactly one open-action->{action} effect for {event}, got {entry.get('effects')}",
            )
            self.assertEqual(opens[0].get("owner"), "user")

    def test_terminal_side_states_land_completed_with_stage_flavour(self):
        table = self._table()
        expected = {
            "cancelled": "Cancelled",
            "returned": "Returned",
            "refunded": "Refunded",
            "delivery-failed": "Delivery-failed",
        }
        for event, stage in expected.items():
            matches = self._match(table, "IN_PROGRESS", event)
            self.assertEqual(
                len(matches), 1,
                f"expected exactly one IN_PROGRESS+{event} transition in orders, got {matches}",
            )
            entry = matches[0]
            self.assertEqual(
                entry.get("to"), "COMPLETED",
                f"{event} is a terminal side-state and must land COMPLETED",
            )
            set_stage = [e for e in entry.get("effects", [])
                         if e.get("op") == "set-stage" and e.get("stage") == stage]
            self.assertEqual(
                len(set_stage), 1,
                f"expected exactly one set-stage->{stage} effect for {event}, got {entry.get('effects')}",
            )


class OrdersEventVerbTest(unittest.TestCase):
    """§S2 — `store.py event orders <id> <event>` drives Mongo docs through the table."""

    def setUp(self):
        # An EMPTY data dir: proves the event verb is not silently reading/writing the
        # real data/*.jsonl files.
        self.data_dir = tempfile.mkdtemp(prefix="oa-cr015-s2-data-")

        self.env = dict(os.environ)
        self.env["VIDUSHI_MONGO_DB"] = TEST_DB
        self.env["VIDUSHI_DATA_DIR"] = self.data_dir
        self.env["VIDUSHI_FORMAT"] = "json"

        self.client = pymongo.MongoClient("mongodb://127.0.0.1:27017", serverSelectionTimeoutMS=2000)
        self.db = self.client[TEST_DB]
        self.db["orders"].create_index("id", unique=True)

    def tearDown(self):
        self.client.drop_database(TEST_DB)
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

    def test_delivered_advances_in_progress_to_completed(self):
        self.db["orders"].insert_one({"id": "ord_e", "merchant": "Acme", "status": "IN_PROGRESS"})

        self._run("event", "orders", "ord_e", "delivered")

        doc = self.db["orders"].find_one({"id": "ord_e"})
        self.assertIsNotNone(doc, "ord_e must still exist in the Mongo test DB")
        self.assertEqual(doc["status"], "COMPLETED")
        self.assertEqual(doc.get("stage"), "Delivered")
        # negative bound: delivered opens no action
        self.assertEqual(doc.get("actions", []), [])

    def test_bogus_event_is_rejected_and_leaves_doc_unchanged(self):
        self.db["orders"].insert_one(
            {"id": "ord_b", "merchant": "Acme", "status": "IN_PROGRESS", "stage": "Shipped"}
        )

        result = self._run("event", "orders", "ord_b", "bogus", expect_success=False)

        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout.strip())
        self.assertIn("illegal transition", payload.get("error", ""))
        # negative bound: the rejected event mutates nothing
        doc = self.db["orders"].find_one({"id": "ord_b"})
        self.assertEqual(doc["status"], "IN_PROGRESS")
        self.assertEqual(doc["stage"], "Shipped")

    def test_shipped_advances_stage_and_holds_in_progress(self):
        self.db["orders"].insert_one({"id": "ord_s", "merchant": "Acme", "status": "IN_PROGRESS"})

        self._run("event", "orders", "ord_s", "shipped")

        doc = self.db["orders"].find_one({"id": "ord_s"})
        self.assertEqual(doc["status"], "IN_PROGRESS")
        self.assertEqual(doc["stage"], "Shipped")
        # negative bound: a stage advance opens no action
        self.assertEqual(doc.get("actions", []), [])

    def test_held_at_customs_opens_action_and_attention_lists_the_row(self):
        self.db["orders"].insert_one({"id": "ord_c", "merchant": "Acme", "status": "IN_PROGRESS"})

        self._run("event", "orders", "ord_c", "held-at-customs")

        doc = self.db["orders"].find_one({"id": "ord_c"})
        self.assertEqual(doc["status"], "IN_PROGRESS")
        opens = [a for a in doc.get("actions", [])
                 if a.get("action") == "customs-clearance" and a.get("status") == "OPEN"]
        self.assertEqual(len(opens), 1, f"expected one OPEN customs-clearance action, got {doc.get('actions')}")

        attn = self._run("attention", "orders")
        rows = json.loads(attn.stdout.strip())
        ids = [r.get("id") for r in rows]
        self.assertIn("ord_c", ids, f"attention orders must surface the customs-held order, got {rows}")

    def test_cancelled_is_a_terminal_side_state(self):
        self.db["orders"].insert_one({"id": "ord_x", "merchant": "Acme", "status": "IN_PROGRESS"})

        self._run("event", "orders", "ord_x", "cancelled")

        doc = self.db["orders"].find_one({"id": "ord_x"})
        self.assertEqual(doc["status"], "COMPLETED")
        self.assertEqual(doc.get("stage"), "Cancelled")


if __name__ == "__main__":
    unittest.main()
