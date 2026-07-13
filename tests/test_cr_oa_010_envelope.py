"""CR-OA-010 Cycle B — AXI ergonomics #4 (pre-computed aggregates), #5
(definitive empty states), and #9 (contextual `next[]`), TOON-only (decision
"B"). The `--json` / `VIDUSHI_FORMAT=json` contract stays a clean, full-data bare
array, byte-stable and envelope-free.

Verifies:
  §S3 — the TOON `query <type>` output carries a pre-computed `count` equal
        to the number of results, so the agent doesn't round-trip a separate
        `stats` call.
  §S4 — a TOON query matching nothing shows an explicit `count: 0` /
        `results: []` marker — a *definitive* empty state, not a bare `[]`
        (and never expressible as a `json.loads`-able JSON array, since the
        TOON path is not JSON).
  §S7 — a TOON `query` response ends with a concise `next[]` block (1-3
        entries) of concrete follow-up command templates relevant to the
        query.
  contract — `--json` (and `VIDUSHI_FORMAT=json`) stays a bare array: no `count`
        wrapper, no `next[]`, no envelope of any kind.

None of the envelope exists in `store.py` yet — `cmd_query` still calls
`out(rows)` with a bare list for the TOON path, so decoding it never yields a
dict with `count`/`results`/`next` keys. Tests 1-3 below MUST fail today
(indexing a list with a string key raises `TypeError`, or the envelope-shape
assertions fail outright); the `--json` contract tests (4) pass today and
continue to guard decision "B" post-GREEN.

DATA SAFETY: every subprocess call points `VIDUSHI_DATA_DIR` at an EMPTY tempdir
(never the real repo `data/`) and `VIDUSHI_MONGO_DB` at `vidushi_oa_test`
(never the real DB), dropped in tearDown. Requires a local mongod on
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

TEST_DB = "vidushi_oa_test"

sys.path.insert(0, SCRIPTS)
import oa_toon  # noqa: E402  (needs sys.path insert above; from_toon for envelope checks)

SUBSCRIPTIONS_SEED = [
    {
        "id": "sub_streamly",
        "provider": "Streamly",
        "category": "streaming",
        "disposition": "KEEP",
        "status": "IN_PROGRESS",
        "renews": "2026-08-01",
    },
    {
        "id": "sub_cloudify",
        "provider": "Cloudify",
        "category": "cloud-storage",
        "disposition": "KEEP",
        "status": "NEW",
        "renews": "2026-09-15",
    },
    {
        "id": "sub_gymflex",
        "provider": "GymFlex",
        "category": "fitness",
        "disposition": "TOMBSTONE",
        "status": "DUE",
        "renews": "2026-07-20",
    },
]


class _BaseSubprocessCase(unittest.TestCase):
    """Shared Mongo + tempdir isolation harness for CLI subprocess tests."""

    def setUp(self):
        self.data_dir = tempfile.mkdtemp(prefix="oa-cr010b-")
        self.env = dict(os.environ)
        self.env["VIDUSHI_MONGO_DB"] = TEST_DB
        self.env["VIDUSHI_DATA_DIR"] = self.data_dir
        self.client = pymongo.MongoClient("mongodb://127.0.0.1:27017", serverSelectionTimeoutMS=2000)
        self.db = self.client[TEST_DB]

    def tearDown(self):
        self.client.drop_database(TEST_DB)
        self.client.close()
        shutil.rmtree(self.data_dir, ignore_errors=True)

    def _run(self, args, extra_env=None):
        env = dict(self.env)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [sys.executable, STORE] + args,
            capture_output=True, text=True, env=env,
        )


class EnvelopeCountTest(_BaseSubprocessCase):
    """§S3 — TOON `query subscriptions` carries a pre-computed `count` equal
    to the number of results."""

    def setUp(self):
        super().setUp()
        self.db["subscriptions"].insert_many([dict(r) for r in SUBSCRIPTIONS_SEED])

    def test_query_count_matches_result_length(self):
        result = self._run(["query", "subscriptions"])
        self.assertEqual(result.returncode, 0, f"query failed: {result.stderr}")

        d = oa_toon.from_toon(result.stdout.strip())
        self.assertIsInstance(d, dict, f"expected an envelope dict, got {type(d).__name__}: {d!r}")
        self.assertIn("results", d)
        self.assertIn("count", d)

        self.assertEqual(d["count"], 3)
        self.assertEqual(len(d["results"]), 3)
        # negative bound: exactly the three seeded ids, nothing dropped/extra
        self.assertEqual(
            sorted(r["id"] for r in d["results"]),
            ["sub_cloudify", "sub_gymflex", "sub_streamly"],
        )

    def test_count_matches_stats_total_for_unfiltered_query(self):
        result = self._run(["query", "subscriptions"])
        self.assertEqual(result.returncode, 0, f"query failed: {result.stderr}")
        d = oa_toon.from_toon(result.stdout.strip())
        self.assertIsInstance(d, dict, f"expected an envelope dict, got {type(d).__name__}: {d!r}")

        stats_result = self._run(["stats", "subscriptions"])
        self.assertEqual(stats_result.returncode, 0, f"stats failed: {stats_result.stderr}")
        stats = oa_toon.from_toon(stats_result.stdout.strip())
        self.assertEqual(d["count"], stats["total"])


class EnvelopeEmptyStateTest(_BaseSubprocessCase):
    """§S4 — a TOON query matching nothing shows a definitive `count: 0` /
    `results: []` marker, never a bare `[]`."""

    def setUp(self):
        super().setUp()
        self.db["subscriptions"].insert_many([dict(r) for r in SUBSCRIPTIONS_SEED])

    def test_no_match_query_shows_definitive_empty_state(self):
        result = self._run(["query", "subscriptions", "--where", "provider=NoSuchProvider"])
        self.assertEqual(result.returncode, 0, f"query failed: {result.stderr}")

        stdout = result.stdout
        # negative: the TOON path is never valid JSON — "nothing matched"
        # must not be expressible as a json.loads-able array on this path.
        with self.assertRaises(json.JSONDecodeError):
            json.loads(stdout)

        d = oa_toon.from_toon(stdout.strip())
        # negative bound: a definitive empty ENVELOPE, not a bare list
        self.assertIsInstance(d, dict, f"expected an envelope dict, got {type(d).__name__}: {d!r}")
        self.assertEqual(d["count"], 0)
        self.assertEqual(d["results"], [])

    def test_matching_query_is_not_mistaken_for_empty(self):
        # positive control: same shape of call, but a filter that DOES match —
        # count must be the non-zero match count, not always 0.
        result = self._run(["query", "subscriptions", "--where", "provider=Streamly"])
        self.assertEqual(result.returncode, 0, f"query failed: {result.stderr}")
        d = oa_toon.from_toon(result.stdout.strip())
        self.assertIsInstance(d, dict, f"expected an envelope dict, got {type(d).__name__}: {d!r}")
        self.assertEqual(d["count"], 1)
        self.assertEqual(len(d["results"]), 1)
        self.assertEqual(d["results"][0]["id"], "sub_streamly")


class EnvelopeNextTest(_BaseSubprocessCase):
    """§S7 — a TOON `query` response ends with a concise `next[]` block of
    concrete, relevant follow-up command templates."""

    def setUp(self):
        super().setUp()
        self.db["subscriptions"].insert_many([dict(r) for r in SUBSCRIPTIONS_SEED])

    def test_query_appends_next_command_templates(self):
        result = self._run(["query", "subscriptions"])
        self.assertEqual(result.returncode, 0, f"query failed: {result.stderr}")

        d = oa_toon.from_toon(result.stdout.strip())
        self.assertIsInstance(d, dict, f"expected an envelope dict, got {type(d).__name__}: {d!r}")
        self.assertIn("next", d)

        next_block = d["next"]
        self.assertIsInstance(next_block, list)
        # positive: a non-empty, concise (spec: 1-3) suggestion block
        self.assertGreaterEqual(len(next_block), 1)
        self.assertLessEqual(len(next_block), 3, f"next[] must stay concise (1-3), got {len(next_block)}: {next_block!r}")
        for tmpl in next_block:
            self.assertIsInstance(tmpl, str)
            self.assertTrue(tmpl.strip(), "next[] entries must not be blank")

        # relevance: at least one template concretely refers to this query's type
        self.assertTrue(
            any("subscriptions" in tmpl for tmpl in next_block),
            f"no next[] template references subscriptions: {next_block!r}",
        )

    def test_next_absent_for_empty_result_or_still_a_bounded_list(self):
        # negative bound: even for zero matches, next[] (if present) stays a
        # list within the 1-3 concise bound — never an unbounded dump.
        result = self._run(["query", "subscriptions", "--where", "provider=NoSuchProvider"])
        self.assertEqual(result.returncode, 0, f"query failed: {result.stderr}")
        d = oa_toon.from_toon(result.stdout.strip())
        self.assertIsInstance(d, dict, f"expected an envelope dict, got {type(d).__name__}: {d!r}")
        self.assertIn("next", d)
        self.assertIsInstance(d["next"], list)
        self.assertLessEqual(len(d["next"]), 3)


class JsonContractStaysBareArrayTest(_BaseSubprocessCase):
    """Contract — `--json` / `VIDUSHI_FORMAT=json` stay a bare array: no `count`
    wrapper, no `next[]`, no envelope of any kind (decision "B"). These PASS
    today; they guard the fork so GREEN cannot leak the envelope into JSON."""

    def setUp(self):
        super().setUp()
        self.db["subscriptions"].insert_many([dict(r) for r in SUBSCRIPTIONS_SEED])

    def test_json_flag_returns_bare_array_no_envelope(self):
        result = self._run(["query", "subscriptions", "--json"])
        self.assertEqual(result.returncode, 0, f"query --json failed: {result.stderr}")

        parsed = json.loads(result.stdout)
        self.assertIsInstance(parsed, list)
        self.assertNotIsInstance(parsed, dict)  # negative: no envelope wrapper
        self.assertEqual(len(parsed), 3)
        for row in parsed:
            self.assertNotIn("count", row)
            self.assertNotIn("next", row)
        self.assertEqual(
            sorted(r["id"] for r in parsed),
            ["sub_cloudify", "sub_gymflex", "sub_streamly"],
        )

    def test_oa_format_json_env_returns_bare_array_no_envelope(self):
        result = self._run(["query", "subscriptions"], extra_env={"VIDUSHI_FORMAT": "json"})
        self.assertEqual(result.returncode, 0, f"VIDUSHI_FORMAT=json query failed: {result.stderr}")

        parsed = json.loads(result.stdout)
        self.assertIsInstance(parsed, list)
        self.assertNotIsInstance(parsed, dict)
        self.assertEqual(len(parsed), 3)
        for row in parsed:
            self.assertNotIn("count", row)
            self.assertNotIn("next", row)

    def test_json_empty_result_stays_bare_empty_array(self):
        # contract: even the "nothing matched" case stays a bare [] under
        # --json — the definitive-empty-state envelope is TOON-only (§S4).
        result = self._run(["query", "subscriptions", "--json", "--where", "provider=NoSuchProvider"])
        self.assertEqual(result.returncode, 0, f"query --json failed: {result.stderr}")
        parsed = json.loads(result.stdout)
        self.assertIsInstance(parsed, list)
        self.assertEqual(parsed, [])


if __name__ == "__main__":
    unittest.main()
