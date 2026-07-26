"""CR-OA-017 §S2 — AXI conformance gap-closure across the read/error verb surface.

RED tests for the four gaps the §S1 audit found (spec §S1 Findings, 2026-07-26):

  - G1 (#6): `get` on a MISSING id returns `null` with exit 0 — it must instead emit a
    structured `error` on stdout and exit 1 (no traceback).
  - G2 (#9): `get` on a hit (TOON) carries no `next[]` contextual-disclosure block.
  - G3 (#4/#9): `attention` (TOON) emits a bare array with no `next[]` next-step block.
  - G4 (#9): `stats` (TOON) emits a bare object with no `next[]` next-step block.

Also guards the decision-B contract for `get` (a `--json` hit is a bare object with no
`next`/`tally`), which must stay true after the fix.

Every subprocess call points `VIDUSHI_DATA_DIR` at an EMPTY tempdir and `VIDUSHI_MONGO_DB`
at `vidushi_oa_test` (dropped in tearDown). Requires local mongod on 127.0.0.1:27017.
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
STORE = os.path.join(ROOT, "scripts", "store.py")
TEST_DB = "vidushi_oa_test"


class AxiConformanceTest(unittest.TestCase):
    def setUp(self):
        self.data_dir = tempfile.mkdtemp(prefix="oa-cr017-")
        self.env = dict(os.environ)
        self.env["VIDUSHI_MONGO_DB"] = TEST_DB
        self.env["VIDUSHI_DATA_DIR"] = self.data_dir
        self.client = pymongo.MongoClient("mongodb://127.0.0.1:27017", serverSelectionTimeoutMS=2000)
        self.db = self.client[TEST_DB]
        self.db["subscriptions"].create_index("id", unique=True)
        self.db["subscriptions"].insert_one(
            {"id": "sub_axi", "provider": "AxiTest", "category": "saas", "status": "NEW"}
        )

    def tearDown(self):
        self.client.drop_database(TEST_DB)
        self.client.close()
        shutil.rmtree(self.data_dir, ignore_errors=True)

    def _run(self, *args, fmt="toon"):
        env = dict(self.env)
        env["VIDUSHI_FORMAT"] = fmt
        return subprocess.run([sys.executable, STORE, *args], capture_output=True, text=True, env=env)

    # G1 (#6) — get on a missing id is a structured error + exit 1
    def test_get_missing_id_is_structured_error_exit1(self):
        r = self._run("get", "subscriptions", "sub_missing")
        self.assertEqual(r.returncode, 1, f"missing-id get must exit 1, got {r.returncode}; stdout={r.stdout!r}")
        self.assertIn("error", r.stdout.lower())
        self.assertNotIn("Traceback", r.stderr)
        self.assertNotEqual(r.stdout.strip(), "null", "missing-id get must not silently return null")

    # G2 (#9) — get hit carries a next-step block (TOON)
    def test_get_hit_has_next_step(self):
        r = self._run("get", "subscriptions", "sub_axi")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("next[", r.stdout, f"get (TOON) must carry a next[] block; got {r.stdout!r}")

    # G3 (#4/#9) — attention carries a next-step block (TOON)
    def test_attention_has_next_step(self):
        r = self._run("attention", "subscriptions")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("next[", r.stdout, f"attention (TOON) must carry a next[] block; got {r.stdout!r}")

    # G4 (#9) — stats carries a next-step block (TOON)
    def test_stats_has_next_step(self):
        r = self._run("stats", "subscriptions")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("next[", r.stdout, f"stats (TOON) must carry a next[] block; got {r.stdout!r}")

    # decision-B guard — get --json hit stays a bare object with no next/tally
    def test_get_json_hit_is_bare_object(self):
        r = self._run("get", "subscriptions", "sub_axi", fmt="json")
        self.assertEqual(r.returncode, 0, r.stderr)
        payload = json.loads(r.stdout)
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload.get("id"), "sub_axi")
        self.assertNotIn("next", payload)
        self.assertNotIn("tally", payload)


if __name__ == "__main__":
    unittest.main()
