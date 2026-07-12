"""CR-OA-010 Cycle C — S6/AXI #8 content-first no-arg live data.

Verifies the design in CR-OA-010 Sec S6: running `store.py` with NO
arguments must print LIVE DATA (the `attention` worklist — rows with an
OPEN action or an attention-status), the executable path, and a short
one-sentence description of what the tool is -- NOT the argparse
usage/error block that argparse's required-subcommand ("cmd") produces
today.

Baseline behaviour TODAY (pre-GREEN):
  - `store.py` (no args) prints `usage: store.py [-h] ...` to stdout and
    exits with returncode 2 (argparse "the following arguments are
    required: cmd"). Tests 1 and 2 below assert the NEW content-first
    behaviour and therefore MUST FAIL against this baseline.
  - `store.py --help` already prints the argparse usage/help and exits 0 —
    that behaviour must be PRESERVED. Test 3 passes today.
  - `store.py stats subscriptions --json` already works today (a concrete
    verb + args is not empty argv) — proves only a truly-empty argv
    triggers the new no-arg path, no accidental hijack of real verbs.
    Test 4 passes today.

DATA SAFETY: every subprocess call points `VIDUSHI_DATA_DIR` at an EMPTY
tempdir (never the real repo `data/`) and `VIDUSHI_MONGO_DB` at
`vidushi_oa_test` (never the real DB), dropped/removed in
tearDown. Requires a local mongod on 127.0.0.1:27017 (office_assistant
instance; CR-OA-001).
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

# Seed one attention-worthy subscription: an OPEN action, matching the
# `$or: [{"actions.status": "OPEN"}, {"status": {"$in": ATTENTION_STATUSES}}]`
# query in store.py's `cmd_attention` (read from store.py:429).
SEED_SUBSCRIPTIONS = [
    {
        "id": "sub_flag",
        "provider": "Acme",
        "status": "IN_PROGRESS",
        "actions": [
            {"action": "cancel-before-charge", "status": "OPEN", "owner": "user"},
        ],
    },
]


class NoArgLiveDataTest(unittest.TestCase):
    """S6/AXI#8 — bare `store.py` (no args) shows live data, not usage."""

    def setUp(self):
        self.data_dir = tempfile.mkdtemp(prefix="oa-cr010c-noarg-")
        self.env = dict(os.environ)
        self.env["VIDUSHI_MONGO_DB"] = TEST_DB
        self.env["VIDUSHI_DATA_DIR"] = self.data_dir

        self.client = pymongo.MongoClient("mongodb://127.0.0.1:27017", serverSelectionTimeoutMS=2000)
        self.db = self.client[TEST_DB]
        self.db["subscriptions"].insert_many([dict(row) for row in SEED_SUBSCRIPTIONS])

    def tearDown(self):
        self.client.drop_database(TEST_DB)
        self.client.close()
        shutil.rmtree(self.data_dir, ignore_errors=True)

    def _run(self, extra_args):
        return subprocess.run(
            [sys.executable, STORE] + extra_args,
            capture_output=True, text=True, env=self.env,
        )

    def test_no_arg_shows_live_data_not_usage(self):
        result = self._run([])

        # POSITIVE: exit 0, not argparse's exit 2.
        self.assertEqual(result.returncode, 0, f"stderr: {result.stderr}")

        stdout = result.stdout
        # NEGATIVE: this is NOT the argparse usage/error block.
        self.assertFalse(
            stdout.startswith("usage:"),
            f"no-arg invocation must not print argparse usage, got: {stdout[:120]!r}",
        )
        self.assertNotIn(
            "error:", stdout,
            f"no-arg invocation must not print an argparse error, got: {stdout[:200]!r}",
        )

        # POSITIVE: the executable path token is surfaced.
        self.assertIn("store.py", stdout)

        # POSITIVE: the seeded attention item (worklist row / its OPEN
        # action) is surfaced somewhere in the output.
        self.assertTrue(
            "sub_flag" in stdout or "cancel-before-charge" in stdout,
            f"expected seeded attention item to appear in no-arg output, got: {stdout!r}",
        )

        # POSITIVE: a short human-readable description of the tool is
        # present (mentions the store/office-assistant domain) and the
        # output is more than a one-liner.
        self.assertGreater(len(stdout.strip()), 40)
        lowered = stdout.lower()
        self.assertTrue(
            "office" in lowered or "store" in lowered,
            f"expected a human-readable description mentioning 'office' or 'store', got: {stdout!r}",
        )

    def test_no_arg_exit_code_is_zero(self):
        result = self._run([])
        self.assertEqual(result.returncode, 0, f"stderr: {result.stderr}")
        # negative bound: not argparse's error exit code
        self.assertNotEqual(result.returncode, 2)

    def test_help_flag_still_prints_argparse_usage(self):
        result = self._run(["--help"])

        self.assertEqual(result.returncode, 0, f"stderr: {result.stderr}")
        self.assertIn("usage:", result.stdout)

    def test_specific_verb_still_works_no_accidental_hijack(self):
        result = self._run(["stats", "subscriptions", "--json"])

        self.assertEqual(result.returncode, 0, f"stderr: {result.stderr}")
        parsed = json.loads(result.stdout)
        self.assertIn("total", parsed)
        # POSITIVE: exactly the one seeded subscription counted.
        self.assertEqual(parsed["total"], 1)
        # NEGATIVE: this is the stats payload, not the no-arg worklist —
        # it must NOT carry an attention-style list of rows.
        self.assertNotIn("results", parsed)


if __name__ == "__main__":
    unittest.main()
