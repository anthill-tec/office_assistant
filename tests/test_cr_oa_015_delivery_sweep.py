"""CR-OA-015 Cycle A — §S3 the `delivery-sweep` verb.

Verifies the §S3 acceptance criteria for the stuck-order sweep (a sibling of
`warranty-sweep` / `due-sweep`):

  - `store.py delivery-sweep [--dry-run]` opens a `stuck-chase` action on every in-flight
    order (`status` in {NEW, UNKNOWN, IN_PROGRESS}) that has STALLED — `last_event_date`
    more than 7 days ago OR a past `eta` (< today).
  - Idempotency is NOT a status filter (a stuck order stays IN_PROGRESS): the query excludes
    orders that already carry an OPEN `stuck-chase`, so a REPEAT sweep opens none.
  - `--dry-run` reports what it WOULD touch but writes nothing.
  - Caller-existence: `--help` lists `delivery-sweep` and a non-test path wires the sweep
    function (argparse `set_defaults`).

Neither `cmd_delivery_sweep` nor its subparser exist yet (CR-OA-015 §S3 is still RED), so
EVERY test here MUST fail: the sweep-run tests because argparse rejects `delivery-sweep`
as an unknown command (rc 2, nothing written); the caller-existence test because neither
`--help` nor the package source mentions `delivery-sweep`/`cmd_delivery_sweep`.

DATA SAFETY: every subprocess call points `VIDUSHI_DATA_DIR` at an EMPTY tempdir (never the
real repo `data/`) and `VIDUSHI_MONGO_DB` at `vidushi_oa_test` (never the real DB), dropped
in tearDown. Requires a local mongod on 127.0.0.1:27017 (the office_assistant instance;
CR-OA-001).
"""
import datetime
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
CLI_SRC = os.path.join(ROOT, "vidushi_oa", "_cli.py")

TEST_DB = "vidushi_oa_test"


def _days_ago(n):
    return (datetime.date.today() - datetime.timedelta(days=n)).isoformat()


def _days_ahead(n):
    return (datetime.date.today() + datetime.timedelta(days=n)).isoformat()


class DeliverySweepTest(unittest.TestCase):
    def setUp(self):
        self.data_dir = tempfile.mkdtemp(prefix="oa-cr015-s3-data-")

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

    def _open_stuck(self, order_id):
        doc = self.db["orders"].find_one({"id": order_id})
        return [a for a in (doc.get("actions") or [])
                if a.get("action") == "stuck-chase" and a.get("status") == "OPEN"]

    def test_stale_last_event_opens_one_stuck_chase_idempotent_and_dry_run(self):
        self.db["orders"].insert_one(
            {"id": "ord_stale", "merchant": "Acme", "status": "IN_PROGRESS",
             "last_event_date": _days_ago(8)}
        )

        # --dry-run reports the row it WOULD chase but writes nothing.
        dry = self._run("delivery-sweep", "--dry-run")
        dry_payload = json.loads(dry.stdout.strip())
        self.assertEqual(dry_payload.get("count"), 1)
        self.assertTrue(dry_payload.get("dry_run"))
        self.assertEqual(self._open_stuck("ord_stale"), [], "--dry-run must not write an action")

        # Real sweep opens exactly one OPEN stuck-chase.
        first = self._run("delivery-sweep")
        first_payload = json.loads(first.stdout.strip())
        self.assertEqual(first_payload.get("count"), 1)
        self.assertIn("ord_stale", first_payload.get("chased", []))
        self.assertEqual(len(self._open_stuck("ord_stale")), 1)

        # A second sweep is idempotent — the open-stuck-chase guard opens none.
        second = self._run("delivery-sweep")
        second_payload = json.loads(second.stdout.strip())
        self.assertEqual(second_payload.get("count"), 0)
        self.assertEqual(second_payload.get("chased", []), [])
        # negative bound: still exactly one stuck-chase (no duplicate pushed)
        self.assertEqual(len(self._open_stuck("ord_stale")), 1)

    def test_past_eta_with_recent_last_event_is_also_chased(self):
        self.db["orders"].insert_one(
            {"id": "ord_eta", "merchant": "Acme", "status": "IN_PROGRESS",
             "last_event_date": _days_ago(1), "eta": _days_ago(2)}
        )

        result = self._run("delivery-sweep")
        payload = json.loads(result.stdout.strip())
        self.assertEqual(payload.get("count"), 1)
        self.assertIn("ord_eta", payload.get("chased", []))
        self.assertEqual(len(self._open_stuck("ord_eta")), 1)

    def test_fresh_and_terminal_orders_are_not_chased(self):
        # fresh, on-track order: recent event, future eta -> not stuck
        self.db["orders"].insert_one(
            {"id": "ord_fresh", "merchant": "Acme", "status": "IN_PROGRESS",
             "last_event_date": _days_ago(1), "eta": _days_ahead(3)}
        )
        # delivered order, long silent -> terminal, excluded by the status filter
        self.db["orders"].insert_one(
            {"id": "ord_done", "merchant": "Acme", "status": "COMPLETED",
             "last_event_date": _days_ago(40)}
        )

        result = self._run("delivery-sweep")
        payload = json.loads(result.stdout.strip())
        self.assertEqual(payload.get("count"), 0, f"no order should be chased, got {payload}")
        self.assertEqual(self._open_stuck("ord_fresh"), [])
        self.assertEqual(self._open_stuck("ord_done"), [])

    def test_help_lists_delivery_sweep_and_a_nontest_path_wires_it(self):
        help_result = subprocess.run(
            [sys.executable, STORE, "--help"], capture_output=True, text=True, env=self.env,
        )
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("delivery-sweep", help_result.stdout)

        # Caller-existence: the sweep function is defined AND wired from a non-test path
        # (argparse set_defaults) — at least two references in the package source.
        with open(CLI_SRC, encoding="utf-8") as f:
            src = f.read()
        self.assertGreaterEqual(
            src.count("cmd_delivery_sweep"), 2,
            "cmd_delivery_sweep must be both defined and wired into the CLI (non-test caller)",
        )


if __name__ == "__main__":
    unittest.main()
