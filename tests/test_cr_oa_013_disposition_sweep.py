"""CR-OA-013 — disposition-aware `due-sweep`.

Today `_apply_transition`'s `subscriptions` `renewal-window` transition
(`vidushi_oa.transitions.TRANSITIONS["subscriptions"][0]`) unconditionally opens
`cancel-before-charge` regardless of the doc's `disposition` — wrong for a `KEEP`
subscription, which should instead get `renewal-confirm` (protect/confirm the
renewal). `insurance` (no `disposition` field) is unaffected and keeps opening
`renew-policy`.

This CR makes the opened action disposition-keyed (`cmd_due_sweep` /
`_apply_transition`, `vidushi_oa/_cli.py`):
  - `disposition:"KEEP"`               -> open `renewal-confirm`, NOT `cancel-before-charge`
  - `disposition:"TOMBSTONE"`/`"UNDECIDED"` (or unset) -> open `cancel-before-charge` (unchanged)
  - `insurance` (no disposition concept) -> unaffected, still opens `renew-policy`
  - idempotent: a second sweep must not duplicate the opened action

No disposition branch exists yet anywhere in the sweep/transition path, so EVERY
behavioral test here (T1-T4) MUST fail against the current implementation: a KEEP
subscription gets `cancel-before-charge` (today's uniform behaviour), never
`renewal-confirm`. T5 (`renewal-confirm` already declared in
`ACTION_SETS["subscriptions"]`) may already pass — that constant predates this CR;
it is included here only to pin/guard it, not because it is expected RED.

DATA SAFETY: every subprocess call points `VIDUSHI_DATA_DIR` at a fresh EMPTY
tempdir (never the real repo `data/`) and `VIDUSHI_MONGO_DB` at `vidushi_oa_test`
(never the real DB). setUp/tearDown `drop_database("vidushi_oa_test")` ONLY —
never the default `vidushi_oa` database. Requires a local mongod on
127.0.0.1:27017 (CR-OA-001).
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

TEST_DB = "vidushi_oa_test"


def _iso(days_from_today):
    return (datetime.date.today() + datetime.timedelta(days=days_from_today)).isoformat()


def _open_action_slugs(doc):
    """The set of OPEN action slugs on a doc after a sweep."""
    return {act.get("action") for act in doc.get("actions", []) if act.get("status") == "OPEN"}


class DueSweepTestCase(unittest.TestCase):
    """Shared subprocess/Mongo harness — mirrors tests/test_cr_oa_007_due_sweep.py."""

    def setUp(self):
        # An EMPTY data dir: due-sweep must operate on Mongo, never the real
        # data/subscriptions.jsonl or data/insurance.jsonl files.
        self.data_dir = tempfile.mkdtemp(prefix="oa-cr013-data-")

        self.env = dict(os.environ)
        self.env["VIDUSHI_MONGO_DB"] = TEST_DB
        self.env["VIDUSHI_DATA_DIR"] = self.data_dir
        self.env["VIDUSHI_FORMAT"] = "json"

        self.client = pymongo.MongoClient("mongodb://127.0.0.1:27017", serverSelectionTimeoutMS=2000)
        self.db = self.client[TEST_DB]

    def tearDown(self):
        # DATA SAFETY: drop ONLY the throwaway test DB, never the live `vidushi_oa`.
        self.client.drop_database(TEST_DB)
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

    def _sub(self, doc_id):
        return self.db["subscriptions"].find_one({"id": doc_id}, {"_id": 0})

    def _ins(self, doc_id):
        return self.db["insurance"].find_one({"id": doc_id}, {"_id": 0})


class KeepSubscriptionOpensRenewalConfirmTest(DueSweepTestCase):
    """§S1 AC1 — a `disposition:"KEEP"` subscription in the lookahead window gets
    `renewal-confirm`, never `cancel-before-charge`."""

    def test_keep_subscription_gets_renewal_confirm_not_cancel_before_charge(self):
        self.db["subscriptions"].insert_one({
            "id": "sub_keep", "provider": "Acme", "status": "IN_PROGRESS",
            "disposition": "KEEP", "renews": _iso(10),
        })

        self._sweep()

        doc = self._sub("sub_keep")
        self.assertIsNotNone(doc, "sub_keep must still exist in the Mongo test DB")
        self.assertEqual(doc["status"], "DUE")

        open_slugs = _open_action_slugs(doc)
        self.assertIn(
            "renewal-confirm", open_slugs,
            f"KEEP subscription must open renewal-confirm, got actions={doc.get('actions', [])}",
        )
        self.assertNotIn(
            "cancel-before-charge", open_slugs,
            f"KEEP subscription must NOT open cancel-before-charge, got actions={doc.get('actions', [])}",
        )
        # positive bound: exactly one action opened, no stray extras
        self.assertEqual(len(doc.get("actions", [])), 1)


class NonKeepSubscriptionsKeepCancelBeforeChargeTest(DueSweepTestCase):
    """§S1 AC2 — TOMBSTONE and UNDECIDED subscriptions keep today's behaviour:
    `cancel-before-charge`, never `renewal-confirm`."""

    def test_tombstone_and_undecided_subscriptions_get_cancel_before_charge(self):
        self.db["subscriptions"].insert_one({
            "id": "sub_tombstone", "provider": "Zeta", "status": "IN_PROGRESS",
            "disposition": "TOMBSTONE", "renews": _iso(10),
        })
        self.db["subscriptions"].insert_one({
            "id": "sub_undecided", "provider": "Omega", "status": "IN_PROGRESS",
            "disposition": "UNDECIDED", "renews": _iso(10),
        })

        self._sweep()

        for doc_id in ("sub_tombstone", "sub_undecided"):
            doc = self._sub(doc_id)
            self.assertIsNotNone(doc, f"{doc_id} must still exist in the Mongo test DB")
            self.assertEqual(doc["status"], "DUE")

            open_slugs = _open_action_slugs(doc)
            self.assertIn(
                "cancel-before-charge", open_slugs,
                f"{doc_id} must open cancel-before-charge, got actions={doc.get('actions', [])}",
            )
            self.assertNotIn(
                "renewal-confirm", open_slugs,
                f"{doc_id} must NOT open renewal-confirm, got actions={doc.get('actions', [])}",
            )
            self.assertEqual(len(doc.get("actions", [])), 1)


class InsuranceUnaffectedByDispositionTest(DueSweepTestCase):
    """§S1 AC3 — insurance has no disposition concept; it must still open
    `renew-policy` (unchanged) when its `expiry` falls in the lookahead."""

    def test_insurance_in_window_still_opens_renew_policy(self):
        self.db["insurance"].insert_one({
            "id": "ins_test", "insurer": "HDFC Ergo", "policy_no": "P1",
            "status": "IN_PROGRESS", "expiry": _iso(10),
        })

        self._sweep()

        doc = self._ins("ins_test")
        self.assertIsNotNone(doc, "ins_test must still exist in the Mongo test DB")
        self.assertEqual(doc["status"], "DUE")

        open_slugs = _open_action_slugs(doc)
        self.assertIn(
            "renew-policy", open_slugs,
            f"insurance doc must open renew-policy, got actions={doc.get('actions', [])}",
        )
        # negative bound: insurance never gets the subscriptions-only slugs
        self.assertNotIn("renewal-confirm", open_slugs)
        self.assertNotIn("cancel-before-charge", open_slugs)
        self.assertEqual(len(doc.get("actions", [])), 1)


class DispositionSweepIdempotentTest(DueSweepTestCase):
    """§S1 AC4 — a second `due-sweep` opens no duplicate action, for both the KEEP
    and the TOMBSTONE branch (the `status != DUE` filter must gate it)."""

    def test_second_sweep_adds_no_duplicate_renewal_confirm_for_keep(self):
        self.db["subscriptions"].insert_one({
            "id": "sub_keep", "provider": "Acme", "status": "IN_PROGRESS",
            "disposition": "KEEP", "renews": _iso(10),
        })

        self._sweep()
        self._sweep()

        doc = self._sub("sub_keep")
        self.assertIsNotNone(doc, "sub_keep must still exist in the Mongo test DB")
        self.assertEqual(doc["status"], "DUE")

        renewal_confirm_open = [
            act for act in doc.get("actions", [])
            if act.get("action") == "renewal-confirm" and act.get("status") == "OPEN"
        ]
        self.assertEqual(
            len(renewal_confirm_open), 1,
            f"expected exactly one OPEN renewal-confirm after two sweeps, got {doc.get('actions', [])}",
        )
        # negative bound: no stray actions of any kind were pushed alongside it
        self.assertEqual(len(doc.get("actions", [])), 1)

    def test_second_sweep_adds_no_duplicate_cancel_before_charge_for_tombstone(self):
        self.db["subscriptions"].insert_one({
            "id": "sub_tombstone", "provider": "Zeta", "status": "IN_PROGRESS",
            "disposition": "TOMBSTONE", "renews": _iso(10),
        })

        self._sweep()
        self._sweep()

        doc = self._sub("sub_tombstone")
        self.assertIsNotNone(doc, "sub_tombstone must still exist in the Mongo test DB")
        self.assertEqual(doc["status"], "DUE")

        cancel_open = [
            act for act in doc.get("actions", [])
            if act.get("action") == "cancel-before-charge" and act.get("status") == "OPEN"
        ]
        self.assertEqual(
            len(cancel_open), 1,
            f"expected exactly one OPEN cancel-before-charge after two sweeps, got {doc.get('actions', [])}",
        )
        self.assertEqual(len(doc.get("actions", [])), 1)


class ActionSetsMembershipTest(unittest.TestCase):
    """`renewal-confirm` must be a declared member of
    `vidushi_oa._cli.ACTION_SETS["subscriptions"]`."""

    def test_renewal_confirm_is_member_of_subscriptions_action_set(self):
        sys.path.insert(0, SCRIPTS)
        import vidushi_oa._cli as cli_mod

        self.assertIn("renewal-confirm", cli_mod.ACTION_SETS["subscriptions"])
        # negative bound: membership doesn't come at the cost of the pre-existing slug
        self.assertIn("cancel-before-charge", cli_mod.ACTION_SETS["subscriptions"])


if __name__ == "__main__":
    unittest.main()
