"""CR-OA-009 Cycle B — §S2 global `--format toon|json` on `store.py`.

Verifies the §S2 ACs: a global `--format` option (values `toon`|`json`, default
`toon`) is honored by every verb — reads (`query`, `get`, `stats`, `attention`,
`validate`) *and* writes (`add`, `update`, `set-status`, `action-add`,
`action-resolve`, `doc-add`, `event`, the sweeps). Every stdout payload routes
through `oa_toon.to_toon()` unless `--json` is given (or `--format json`), in
which case today's exact `json.dumps` output is preserved.

This module covers the `query` and `add` verbs against the `subscriptions`
store as the representative read/write pair:

  - default (no `--format`) query output is a TOON tabular block: a header
    line `[N,]{fields}:` followed by N indented data rows — NOT a JSON array;
  - the bare `--json` flag (global format switch) on `query` preserves the
    pre-CR strict JSON array;
  - the TOON default and the `--json` output are lossless round-trips of the
    same underlying rows (`oa_toon.from_toon(toon) == json.loads(json)`);
  - `--format json` is equivalent to the bare `--json` flag;
  - `--format` is a REAL argparse choice option (`toon`/`json` accepted,
    anything else rejected with a nonzero exit and an "invalid choice"
    message) — not merely absent (which would ALSO reject any `--format ...`
    invocation, so the test asserts both the accept AND the reject side);
  - a write verb (`add`) emits a TOON status object by default too (format is
    not reads-only).

CR-OA-009 Cycle B has NOT been implemented yet (`store.py` still emits only
JSON and has no `--format`/global `--json` option), so EVERY test here MUST
fail: the TOON-shape assertions fail because today's default output is JSON,
the `--json`/`--format json` reads fail because `query` doesn't accept either
flag yet (argparse errors -> empty stdout -> `json.loads` raises), and the
`--format` choice test fails because even the *valid* `--format toon` value is
rejected today (unrecognized argument).

NOTE (CR-OA-010 Cycle B, decision "B"): `query`'s default TOON output is now a
`{count, results, next}` envelope, superseding this CR's original bare
tabular-header contract for that verb. The two `query`-TOON assertions below
were updated to decode the envelope (`oa_toon.from_toon(stdout)["results"]`)
instead of matching a bare `[N,]{...}:` header line; the `--json` assertions
are untouched (still a bare array, decision "B").

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
import oa_toon  # noqa: E402  (needs sys.path insert above; from_toon for lossless checks)


SEED_SUBSCRIPTIONS = [
    {
        "id": "sub_a",
        "provider": "Acme",
        "disposition": "KEEP",
        "status": "IN_PROGRESS",
        "renews": "2026-09-01",
    },
    {
        "id": "sub_b",
        "provider": "Beta",
        "disposition": "KEEP",
        "status": "IN_PROGRESS",
        "renews": "2026-09-02",
    },
    {
        "id": "sub_c",
        "provider": "Gamma",
        "disposition": "TOMBSTONE",
        "status": "DUE",
        "renews": "2026-09-03",
    },
]


class SubscriptionsFormatQueryTest(unittest.TestCase):
    """§S2 — `query` (a read verb) honors the default-TOON / `--json` /
    `--format json` / `--format toon` contract."""

    def setUp(self):
        self.data_dir = tempfile.mkdtemp(prefix="oa-cr009b-query-")
        self.env = dict(os.environ)
        self.env["OA_MONGO_DB"] = TEST_DB
        self.env["OA_DATA_DIR"] = self.data_dir

        self.client = pymongo.MongoClient("mongodb://127.0.0.1:27017", serverSelectionTimeoutMS=2000)
        self.db = self.client[TEST_DB]
        self.db["subscriptions"].insert_many([dict(row) for row in SEED_SUBSCRIPTIONS])

    def tearDown(self):
        self.client.drop_database(TEST_DB)
        self.client.close()
        shutil.rmtree(self.data_dir, ignore_errors=True)

    def _query(self, extra_args):
        return subprocess.run(
            [sys.executable, STORE, "query", "subscriptions"] + extra_args,
            capture_output=True, text=True, env=self.env,
        )

    def test_query_default_output_is_toon_with_header_and_three_rows(self):
        result = self._query(["--fields", "id,provider,disposition,status,renews"])

        self.assertEqual(result.returncode, 0, f"query failed: {result.stderr}")

        d = oa_toon.from_toon(result.stdout.strip())
        self.assertIsInstance(
            d, dict,
            f"default query TOON output must be a {{count, results, next}} envelope, "
            f"got {type(d).__name__}: {d!r}",
        )
        self.assertEqual(d["count"], 3)
        self.assertEqual(
            len(d["results"]), 3,
            f"expected exactly 3 results, got {len(d['results'])}: {d['results']}",
        )
        for row in d["results"]:
            self.assertEqual(set(row.keys()), {"id", "provider", "disposition", "status", "renews"})
        # negative bound: exactly the seeded three ids, nothing extra/missing
        self.assertEqual(sorted(r["id"] for r in d["results"]), ["sub_a", "sub_b", "sub_c"])
        # negative bound: default output must NOT be parseable as a JSON array
        with self.assertRaises(json.JSONDecodeError):
            json.loads(result.stdout)

    def test_query_bare_json_flag_emits_strict_json_array_of_three(self):
        result = self._query(["--fields", "id,provider,disposition,status,renews", "--json"])

        self.assertEqual(result.returncode, 0, f"query --json failed: {result.stderr}")
        parsed = json.loads(result.stdout)
        self.assertIsInstance(parsed, list)
        self.assertEqual(len(parsed), 3)
        for row in parsed:
            self.assertEqual(set(row.keys()), {"id", "provider", "disposition", "status", "renews"})
        # negative bound: exactly the seeded three ids, nothing extra/missing
        self.assertEqual(sorted(r["id"] for r in parsed), ["sub_a", "sub_b", "sub_c"])

    def test_toon_full_output_losslessly_matches_json_equivalent(self):
        # NOTE (CR-OA-010 Cycle B): default `query` TOON is now an envelope
        # AND minimal/truncated (§S1/§S2), so it is intentionally NOT
        # byte-equal to the JSON bare array anymore. The real losslessness
        # guarantee now lives at `--full`, where both the projection and the
        # truncation are disabled — this test was repurposed to assert THAT.
        toon_result = self._query(["--full"])
        json_result = self._query(["--full", "--json"])

        self.assertEqual(toon_result.returncode, 0, f"toon query --full failed: {toon_result.stderr}")
        self.assertEqual(json_result.returncode, 0, f"json query --full failed: {json_result.stderr}")

        d = oa_toon.from_toon(toon_result.stdout.strip())
        self.assertIsInstance(
            d, dict,
            f"--full query TOON output must still be a {{count, results, next}} envelope, "
            f"got {type(d).__name__}: {d!r}",
        )
        decoded_from_toon_results = d["results"]
        decoded_from_json = json.loads(json_result.stdout)

        self.assertEqual(decoded_from_toon_results, decoded_from_json)
        # negative bound: not a truncated/partial match
        self.assertEqual(len(decoded_from_toon_results), 3)

    def test_format_json_flag_equivalent_to_bare_json_flag(self):
        result = self._query(["--fields", "id", "--format", "json"])

        self.assertEqual(result.returncode, 0, f"query --format json failed: {result.stderr}")
        parsed = json.loads(result.stdout)
        self.assertIsInstance(parsed, list)
        self.assertEqual(len(parsed), 3)
        self.assertEqual(sorted(r["id"] for r in parsed), ["sub_a", "sub_b", "sub_c"])

    def test_format_option_is_real_choice_flag_rejecting_invalid_values(self):
        valid_toon = self._query(["--fields", "id", "--format", "toon"])
        self.assertEqual(
            valid_toon.returncode, 0,
            f"--format toon must be accepted as a real choice: {valid_toon.stderr}",
        )
        # negative bound: the accepted run must actually have produced output
        self.assertTrue(valid_toon.stdout.strip())

        invalid = self._query(["--fields", "id", "--format", "xml"])
        self.assertNotEqual(
            invalid.returncode, 0,
            "--format xml must be rejected as an invalid --format choice",
        )
        self.assertIn("invalid choice", invalid.stderr.lower())


class SubscriptionsFormatWriteTest(unittest.TestCase):
    """§S2 — `add` (a write verb) also defaults to TOON stdout, proving
    `--format` is honored beyond reads-only."""

    def setUp(self):
        self.data_dir = tempfile.mkdtemp(prefix="oa-cr009b-write-")
        self.env = dict(os.environ)
        self.env["OA_MONGO_DB"] = TEST_DB
        self.env["OA_DATA_DIR"] = self.data_dir

        self.client = pymongo.MongoClient("mongodb://127.0.0.1:27017", serverSelectionTimeoutMS=2000)
        self.db = self.client[TEST_DB]

    def tearDown(self):
        self.client.drop_database(TEST_DB)
        self.client.close()
        shutil.rmtree(self.data_dir, ignore_errors=True)

    def test_add_default_output_is_toon_with_added_key(self):
        # NOTE: `--json` here is the ADD verb's existing INPUT-payload flag,
        # not the format switch — no `--format`/format-`--json` is passed, so
        # this exercises the default (TOON) output path for a write verb.
        result = subprocess.run(
            [sys.executable, STORE, "add", "subscriptions", "--json", json.dumps({"provider": "Zeta"})],
            capture_output=True, text=True, env=self.env,
        )

        self.assertEqual(result.returncode, 0, f"add failed: {result.stderr}")
        decoded = oa_toon.from_toon(result.stdout.strip())

        self.assertIsInstance(decoded, dict)
        self.assertIn("added", decoded)
        self.assertEqual(decoded["added"], ["sub_zeta"])
        self.assertIn("skipped", decoded)
        # negative bound: nothing was skipped as a dupe on a fresh DB
        self.assertEqual(decoded["skipped"], [])

        # confirm the write actually landed (this isn't just a formatting no-op)
        doc = self.db["subscriptions"].find_one({"id": "sub_zeta"})
        self.assertIsNotNone(doc, "sub_zeta must exist in the Mongo test DB")
        self.assertEqual(doc["provider"], "Zeta")


if __name__ == "__main__":
    unittest.main()
