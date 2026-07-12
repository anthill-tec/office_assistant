"""CR-OA-009 Cycle C — §S3 `OA_FORMAT` env var sets the default output format.

Verifies the §S3 AC: an `OA_FORMAT` env var participates in the format
resolution store.py's `main()` performs into the module global `_FMT`, with
precedence:

    explicit `--format` flag  >  `--json` shortcut  >  `OA_FORMAT` env  >  "toon" (default)

An unrecognised `OA_FORMAT` value falls back to `"toon"` silently (unlike an
invalid `--format <value>` flag, which argparse still rejects with a nonzero
exit). This mirrors the existing `OA_MONGO_URI`/`OA_MONGO_DB`/`OA_DATA_DIR`
env-var pattern (see `scripts/oa_mongo.py`, `scripts/store.py` `DATA`).

CR-OA-009 Cycle C has NOT been implemented yet — `store.py`'s `main()` never
reads `OA_FORMAT`, so the module always falls back to the argparse `--format`
default of `"toon"` regardless of the environment. This means:

  - test 1 (OA_FORMAT=json, no --format -> JSON output) MUST fail today: the
    query still emits TOON, so `json.loads(stdout)` raises;
  - test 3 (no OA_FORMAT, no flag -> TOON) and test 4 (garbage OA_FORMAT ->
    TOON fallback) happen to pass today "by accident" since TOON is always
    the outcome pre-CR — kept for regression coverage of the *other* ends of
    the precedence chain once Cycle C lands;
  - test 2 (explicit --format toon overrides OA_FORMAT=json) also happens to
    pass today by accident (the flag already always wins over env, since env
    isn't consulted at all yet) — kept to guard the precedence order.

NOTE (CR-OA-010 Cycle B, decision "B"): `query`'s TOON output is now a
`{count, results, next}` envelope, superseding the bare `[N,]{...}:` header
this file originally asserted for the TOON side of the precedence chain. The
three tests that assert a TOON default now decode via
`oa_toon.from_toon(stdout)` and check for a dict with a `results` list,
instead of matching a bare tabular header line. The JSON-path assertions are
unchanged.

DATA SAFETY: every subprocess call points `OA_DATA_DIR` at an EMPTY tempdir
(never the real repo `data/`) and `OA_MONGO_DB` at `office_assistant_test`
(never the real DB), which is dropped in tearDown. Requires a local mongod on
127.0.0.1:27017 (the office_assistant instance; CR-OA-001).
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

TEST_DB = "office_assistant_test"

sys.path.insert(0, SCRIPTS)
import oa_toon  # noqa: E402  (needs sys.path insert above)


SEED_SUBSCRIPTIONS = [
    {
        "id": "sub_a",
        "provider": "Acme",
        "status": "IN_PROGRESS",
    },
    {
        "id": "sub_b",
        "provider": "Beta",
        "status": "IN_PROGRESS",
    },
]

# Bare-TOON tabular header the JSON-path tests check for ABSENCE of (negative
# bound: the JSON default must not look like the old bare-array TOON shape).
TOON_HEADER_ID_PROVIDER = r"^\[2[,]?\]\{id,provider\}:"


class OaFormatEnvTest(unittest.TestCase):
    """§S3 — `OA_FORMAT` env var default, honouring the full precedence chain."""

    def setUp(self):
        self.data_dir = tempfile.mkdtemp(prefix="oa-cr009c-env-")
        self.env = dict(os.environ)
        self.env["OA_MONGO_DB"] = TEST_DB
        self.env["OA_DATA_DIR"] = self.data_dir
        # Ensure a clean slate regardless of the ambient shell's env.
        self.env.pop("OA_FORMAT", None)

        self.client = pymongo.MongoClient("mongodb://127.0.0.1:27017", serverSelectionTimeoutMS=2000)
        self.db = self.client[TEST_DB]
        self.db["subscriptions"].insert_many([dict(row) for row in SEED_SUBSCRIPTIONS])

    def tearDown(self):
        self.client.drop_database(TEST_DB)
        self.client.close()
        shutil.rmtree(self.data_dir, ignore_errors=True)

    def _query(self, extra_args, env):
        return subprocess.run(
            [sys.executable, STORE, "query", "subscriptions"] + extra_args,
            capture_output=True, text=True, env=env,
        )

    def test_oa_format_json_env_makes_json_the_default(self):
        env = dict(self.env)
        env["OA_FORMAT"] = "json"

        result = self._query(["--fields", "id,provider"], env)

        self.assertEqual(result.returncode, 0, f"query failed: {result.stderr}")
        parsed = json.loads(result.stdout)
        self.assertIsInstance(parsed, list)
        self.assertEqual(len(parsed), 2)
        for row in parsed:
            self.assertEqual(set(row.keys()), {"id", "provider"})
        # negative bound: exactly the seeded two ids, nothing extra/missing
        self.assertEqual(sorted(r["id"] for r in parsed), ["sub_a", "sub_b"])
        # negative bound: this is NOT the TOON header shape
        self.assertNotRegex(result.stdout.splitlines()[0] if result.stdout.splitlines() else "",
                             TOON_HEADER_ID_PROVIDER)

    def test_explicit_format_flag_overrides_oa_format_env(self):
        env = dict(self.env)
        # Precondition: the env var really is set to the opposite of what we expect to win.
        env["OA_FORMAT"] = "json"
        self.assertEqual(env["OA_FORMAT"], "json")

        result = self._query(["--fields", "id,provider", "--format", "toon"], env)

        self.assertEqual(result.returncode, 0, f"query failed: {result.stderr}")
        # negative bound: NOT valid JSON (i.e. env=json did not leak through)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(result.stdout)

        d = oa_toon.from_toon(result.stdout.strip())
        self.assertIsInstance(
            d, dict,
            f"explicit --format toon must win over OA_FORMAT=json (envelope dict), "
            f"got {type(d).__name__}: {d!r}",
        )
        self.assertIn("results", d)
        self.assertIsInstance(d["results"], list)
        # cross-check via lossless TOON decode -> exactly the 2 seeded rows
        self.assertEqual(len(d["results"]), 2)
        self.assertEqual(sorted(r["id"] for r in d["results"]), ["sub_a", "sub_b"])

    def test_no_oa_format_no_flag_defaults_to_toon(self):
        env = dict(self.env)
        env.pop("OA_FORMAT", None)
        self.assertNotIn("OA_FORMAT", env)

        result = self._query(["--fields", "id"], env)

        self.assertEqual(result.returncode, 0, f"query failed: {result.stderr}")
        with self.assertRaises(json.JSONDecodeError):
            json.loads(result.stdout)

        d = oa_toon.from_toon(result.stdout.strip())
        self.assertIsInstance(
            d, dict,
            f"default (no OA_FORMAT, no --format) must be a TOON envelope dict, "
            f"got {type(d).__name__}: {d!r}",
        )
        self.assertIn("results", d)
        self.assertIsInstance(d["results"], list)
        self.assertEqual(len(d["results"]), 2)
        self.assertEqual(sorted(r["id"] for r in d["results"]), ["sub_a", "sub_b"])

    def test_invalid_oa_format_falls_back_to_toon_without_crashing(self):
        env = dict(self.env)
        env["OA_FORMAT"] = "xml"  # garbage value

        result = self._query(["--fields", "id"], env)

        # negative bound: garbage OA_FORMAT must NOT crash the process, unlike an
        # invalid --format flag (which argparse legitimately rejects).
        self.assertEqual(
            result.returncode, 0,
            f"invalid OA_FORMAT=xml must fall back silently, not crash: rc={result.returncode} stderr={result.stderr!r}",
        )
        with self.assertRaises(json.JSONDecodeError):
            json.loads(result.stdout)

        d = oa_toon.from_toon(result.stdout.strip())
        self.assertIsInstance(
            d, dict,
            f"invalid OA_FORMAT must fall back to a TOON envelope dict, "
            f"got {type(d).__name__}: {d!r}",
        )
        self.assertIn("results", d)
        self.assertIsInstance(d["results"], list)
        self.assertEqual(len(d["results"]), 2)
        self.assertEqual(sorted(r["id"] for r in d["results"]), ["sub_a", "sub_b"])


if __name__ == "__main__":
    unittest.main()
