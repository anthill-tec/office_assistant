"""CR-OA-011 Cycle A — §S3/§S4 hard-cut env + DB-name rename (NO back-compat aliases).

Verifies the new-contract ACs before the rename lands in `scripts/oa_mongo.py` /
`scripts/store.py`:

  - `oa_mongo._DEFAULT_DB == "vidushi_oa"` (was `"office_assistant"`).
  - `VIDUSHI_MONGO_DB` is honoured by `oa_mongo.db()`.
  - the OLD `OA_MONGO_DB` is a dead env var — it is NOT read any more (hard cut,
    no alias): with only `OA_MONGO_DB` set, `oa_mongo.db()` still returns the
    NEW default `"vidushi_oa"`.
  - `VIDUSHI_FORMAT` is honoured by `store.py`'s format-resolution precedence;
    the OLD `OA_FORMAT` is dead (falls through to the TOON default, not JSON).
  - `VIDUSHI_DATA_DIR` is honoured by `store.py snapshot` (old `OA_DATA_DIR` is
    not consulted at all any more).
  - no `scripts/*.py` file still references `OA_MONGO`, `OA_DATA`, or `OA_FORMAT`
    (proof of the hard cut, not just "new names also work").

Today `scripts/oa_mongo.py` still defines `_DEFAULT_DB = "office_assistant"` and
reads `OA_MONGO_URI`/`OA_MONGO_DB`; `scripts/store.py` still reads `OA_DATA_DIR`
and `OA_FORMAT`. Every test in this module MUST fail (or the module MUST fail to
even satisfy its assertions) until CR-OA-011 Cycle A's GREEN phase renames the
env vars + default DB name with a hard cut (no aliasing of the old names).

DATA SAFETY: isolates via `VIDUSHI_MONGO_DB=vidushi_oa_test` (dropped in
tearDown) and a throwaway `tempfile.mkdtemp()` for `VIDUSHI_DATA_DIR` (removed
in tearDown). Requires a local mongod on 127.0.0.1:27017 (the office_assistant
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

sys.path.insert(0, SCRIPTS)
import oa_mongo  # noqa: E402  (needs sys.path insert above)

NEW_DEFAULT_DB = "vidushi_oa"
TEST_DB = "vidushi_oa_test"


class RenameHardCutTest(unittest.TestCase):
    """§S3 — `OA_*` env vars -> `VIDUSHI_*`, no back-compat aliases."""

    def setUp(self):
        self._saved_env = dict(os.environ)
        # Clean slate: neither family of vars ambient from the shell.
        for var in ("OA_MONGO_URI", "OA_MONGO_DB", "OA_DATA_DIR", "OA_FORMAT",
                    "VIDUSHI_MONGO_URI", "VIDUSHI_MONGO_DB", "VIDUSHI_DATA_DIR", "VIDUSHI_FORMAT"):
            os.environ.pop(var, None)
        self._reset_oa_mongo_client()  # drop any cached client from a prior test's env

        self.client = pymongo.MongoClient("mongodb://127.0.0.1:27017", serverSelectionTimeoutMS=2000)
        self.tmpdir = tempfile.mkdtemp(prefix="oa-cr011a-rename-")

    def tearDown(self):
        for db_name in (TEST_DB, "oa_probe", "vidushi_oa_probe"):
            self.client.drop_database(db_name)
        self.client.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        self._reset_oa_mongo_client()
        os.environ.clear()
        os.environ.update(self._saved_env)

    @staticmethod
    def _reset_oa_mongo_client():
        """Close + drop oa_mongo's process-wide cached client so the next db()/client()
        call re-reads the (possibly just-changed) env vars instead of returning a stale
        connection, and so we don't leak an unclosed MongoClient between tests."""
        if oa_mongo._client is not None:
            oa_mongo._client.close()
            oa_mongo._client = None

    def _base_env(self):
        env = dict(os.environ)
        env["VIDUSHI_MONGO_DB"] = TEST_DB
        env["VIDUSHI_DATA_DIR"] = self.tmpdir
        return env

    # ---- 1. new default DB name constant ----

    def test_default_db_constant_is_vidushi_oa(self):
        self.assertEqual(oa_mongo._DEFAULT_DB, "vidushi_oa")
        # negative bound: the old product-name default is gone
        self.assertNotEqual(oa_mongo._DEFAULT_DB, "office_assistant")

    # ---- 2. VIDUSHI_MONGO_DB honoured ----

    def test_vidushi_mongo_db_env_is_honoured(self):
        os.environ["VIDUSHI_MONGO_DB"] = "vidushi_oa_probe"
        self._reset_oa_mongo_client()
        try:
            self.assertEqual(oa_mongo.db().name, "vidushi_oa_probe")
        finally:
            del os.environ["VIDUSHI_MONGO_DB"]
            self._reset_oa_mongo_client()
            self.client.drop_database("vidushi_oa_probe")

    # ---- 3. OA_MONGO_DB is a dead var (hard cut, no alias) ----

    def test_old_oa_mongo_db_env_is_no_longer_honoured(self):
        # Only the OLD var is set; VIDUSHI_MONGO_DB is absent.
        os.environ["OA_MONGO_DB"] = "oa_probe"
        self.assertNotIn("VIDUSHI_MONGO_DB", os.environ)
        self._reset_oa_mongo_client()
        try:
            # positive: falls through to the NEW default, proving OA_MONGO_DB is ignored
            self.assertEqual(oa_mongo.db().name, NEW_DEFAULT_DB)
            # negative: definitely not the value the dead var would have selected
            self.assertNotEqual(oa_mongo.db().name, "oa_probe")
        finally:
            del os.environ["OA_MONGO_DB"]
            self._reset_oa_mongo_client()

    # ---- 4. VIDUSHI_FORMAT honoured / OA_FORMAT dead ----

    def _seed_one_subscription(self):
        self.client.drop_database(TEST_DB)
        self.client[TEST_DB]["subscriptions"].insert_one(
            {"id": "sub_cr011a", "provider": "Acme", "status": "IN_PROGRESS"}
        )

    def _query(self, env):
        return subprocess.run(
            [sys.executable, STORE, "query", "subscriptions", "--fields", "id"],
            capture_output=True, text=True, env=env,
        )

    def test_vidushi_format_json_is_honoured(self):
        self._seed_one_subscription()
        env = self._base_env()
        env["VIDUSHI_FORMAT"] = "json"

        result = self._query(env)

        self.assertEqual(result.returncode, 0, f"query failed: {result.stderr}")
        parsed = json.loads(result.stdout)  # must not raise -> proves JSON was emitted
        self.assertIsInstance(parsed, list)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["id"], "sub_cr011a")

    def test_old_oa_format_env_is_no_longer_honoured(self):
        self._seed_one_subscription()
        env = self._base_env()
        env["OA_FORMAT"] = "json"
        self.assertNotIn("VIDUSHI_FORMAT", env)

        result = self._query(env)

        self.assertEqual(result.returncode, 0, f"query failed: {result.stderr}")
        # negative: OA_FORMAT=json must NOT have produced JSON (dead var) ->
        # stdout must NOT be JSON-loadable (it's TOON, an envelope header + rows)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(result.stdout)
        # positive bound: still valid, non-empty TOON output (the fallback default)
        self.assertTrue(result.stdout.strip())

    # ---- 5. VIDUSHI_DATA_DIR honoured ----

    def test_vidushi_data_dir_env_is_honoured(self):
        self.client.drop_database(TEST_DB)
        self.client[TEST_DB]["subscriptions"].insert_one(
            {"id": "sub_cr011a_snap", "provider": "Beta", "status": "NEW"}
        )
        env = self._base_env()

        result = subprocess.run(
            [sys.executable, STORE, "snapshot", "subscriptions"],
            capture_output=True, text=True, env=env,
        )

        self.assertEqual(result.returncode, 0, f"snapshot failed: {result.stderr}")
        target = os.path.join(self.tmpdir, "subscriptions.jsonl")
        self.assertTrue(os.path.exists(target), f"expected snapshot at {target}")
        with open(target, encoding="utf-8") as f:
            lines = [line for line in f if line.strip()]
        self.assertEqual(len(lines), 1)
        rec = json.loads(lines[0])
        self.assertEqual(rec["id"], "sub_cr011a_snap")
        # negative bound: nothing written under a stray OA_DATA_DIR-shaped path
        stray = os.path.join(self.tmpdir, "..", "OA_DATA_DIR-should-not-exist")
        self.assertFalse(os.path.exists(stray))

    # ---- 6. no OA_ names left anywhere in scripts/*.py (hard-cut proof) ----

    def test_no_oa_env_names_remain_in_scripts(self):
        py_files = sorted(
            os.path.join(SCRIPTS, fn) for fn in os.listdir(SCRIPTS) if fn.endswith(".py")
        )
        self.assertTrue(py_files, "expected scripts/*.py to be non-empty")
        result = subprocess.run(
            ["grep", "-E", "OA_MONGO|OA_DATA|OA_FORMAT", *py_files],
            capture_output=True, text=True,
        )
        # grep returncode 1 == no matches found (what we want); 0 == matches found (fail)
        self.assertEqual(
            result.returncode, 1,
            f"OA_MONGO/OA_DATA/OA_FORMAT still referenced in scripts/*.py:\n{result.stdout}",
        )
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
