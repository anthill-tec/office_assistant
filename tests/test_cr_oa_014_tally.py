"""CR-OA-014 — aggregate `tally` in the TOON query envelope.

Verifies §S1: the TOON `query <type>` envelope (`{count, results, next}` from
CR-OA-010) gains a `tally` — a `by-status` count map computed from the
returned results (no extra Mongo round-trip): `{count, tally:{status:{...}},
results, next}`. Where a store carries a cheap natural second axis, it's
included too (`tally.acct` on all stores that carry `acct`; `tally.disposition`
on subscriptions). An empty result shows `count:0` with an empty/all-zero
`tally` (no crash). The `--json` / `VIDUSHI_FORMAT=json` output is UNCHANGED —
a bare array, no `tally` anywhere (decision "B", byte-stable contract).

Today `cmd_query`'s TOON path (`vidushi_oa/_cli.py`) builds
`{"count": len(rows), "results": rows, "next": _query_next(...)}` — there is
no `tally` key at all. Every behavioral tally assertion below (T1-T3) MUST
fail now: `"tally" in d` is False, so `d["tally"]` raises `KeyError` or the
`assertIn` itself fails. T4 (the `--json` contract) already passes today and
must stay green post-GREEN — it guards decision "B" so the envelope never
leaks into the byte-stable JSON path.

Note: `invoices`' TOON `DEFAULT_FIELDS` (`vidushi_oa/_cli.py`) is
`["id", "vendor", "number", "amount", "date"]` — it does NOT include
`status`/`acct`. That means the *shaped* TOON rows drop those fields before
printing. For `tally.status`/`tally.acct` on invoices to be correct, GREEN
must compute the tally from the fetched Mongo docs (or an unshaped copy),
NOT from the already-field-shaped `results` — T2 pins this down.

DATA SAFETY: every subprocess call points `VIDUSHI_DATA_DIR` at an EMPTY
tempdir (never the real repo `data/`) and `VIDUSHI_MONGO_DB` at
`vidushi_oa_test` (never the real DB), dropped in tearDown ONLY. Requires a
local mongod on 127.0.0.1:27017 (the office_assistant instance; CR-OA-001).
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
    {
        "id": "sub_musicbox",
        "provider": "MusicBox",
        "category": "streaming",
        "disposition": "UNDECIDED",
        "status": "NEW",
        "renews": "2026-10-01",
    },
]

INVOICES_SEED = [
    {
        "id": "doc_acme_1",
        "vendor": "Acme",
        "number": "1",
        "amount": 100,
        "date": "2026-01-01",
        "status": "NEW",
        "acct": "personal",
    },
    {
        "id": "doc_beta_2",
        "vendor": "Beta",
        "number": "2",
        "amount": 200,
        "date": "2026-02-01",
        "status": "IN_PROGRESS",
        "acct": "business",
    },
    {
        "id": "doc_gamma_3",
        "vendor": "Gamma",
        "number": "3",
        "amount": 300,
        "date": "2026-03-01",
        "status": "NEW",
        "acct": "personal",
    },
    {
        "id": "doc_delta_4",
        "vendor": "Delta",
        "number": "4",
        "amount": 400,
        "date": "2026-04-01",
        "status": "COMPLETED",
        "acct": "business",
    },
]


def _find_key_anywhere(obj, key):
    """Recursively search a decoded JSON structure for `key` at any depth.
    Used to prove the byte-stable `--json` contract never leaks a `tally`."""
    if isinstance(obj, dict):
        if key in obj:
            return True
        return any(_find_key_anywhere(v, key) for v in obj.values())
    if isinstance(obj, list):
        return any(_find_key_anywhere(v, key) for v in obj)
    return False


class _BaseSubprocessCase(unittest.TestCase):
    """Shared Mongo + tempdir isolation harness for CLI subprocess tests."""

    def setUp(self):
        self.data_dir = tempfile.mkdtemp(prefix="oa-cr014-")
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


class SubscriptionsTallyTest(_BaseSubprocessCase):
    """§S1 — `query subscriptions` (TOON) carries `tally.status` (sums to
    `count`) and `tally.disposition` (subscriptions carry `disposition`)."""

    def setUp(self):
        super().setUp()
        self.db["subscriptions"].insert_many([dict(r) for r in SUBSCRIPTIONS_SEED])

    def test_status_tally_sums_to_count_and_matches_seeded_mix(self):
        result = self._run(["query", "subscriptions"])
        self.assertEqual(result.returncode, 0, f"query failed: {result.stderr}")

        d = oa_toon.from_toon(result.stdout.strip())
        self.assertIsInstance(d, dict, f"expected an envelope dict, got {type(d).__name__}: {d!r}")
        self.assertEqual(d["count"], 4)
        self.assertIn("tally", d, "TOON envelope must carry a top-level 'tally' key")

        tally = d["tally"]
        self.assertIsInstance(tally, dict)
        self.assertIn("status", tally, "tally must carry a by-status breakdown")

        status_tally = tally["status"]
        # positive: exact per-status counts from the seeded mix (2xNEW, 1xIN_PROGRESS, 1xDUE)
        self.assertEqual(status_tally, {"NEW": 2, "IN_PROGRESS": 1, "DUE": 1})
        # positive + negative bound: values sum to exactly `count`, nothing dropped/extra
        self.assertEqual(sum(status_tally.values()), d["count"])
        # negative: a status never seeded must not appear at all
        self.assertNotIn("COMPLETED", status_tally)
        self.assertNotIn("EXPIRED", status_tally)

    def test_disposition_tally_present_with_positive_counts(self):
        result = self._run(["query", "subscriptions"])
        self.assertEqual(result.returncode, 0, f"query failed: {result.stderr}")

        d = oa_toon.from_toon(result.stdout.strip())
        self.assertIsInstance(d, dict, f"expected an envelope dict, got {type(d).__name__}: {d!r}")
        self.assertIn("tally", d)
        tally = d["tally"]
        self.assertIn("disposition", tally, "subscriptions carry 'disposition' — tally must include it")

        disposition_tally = tally["disposition"]
        # positive: exact per-disposition counts from the seeded mix
        self.assertEqual(disposition_tally, {"KEEP": 2, "TOMBSTONE": 1, "UNDECIDED": 1})
        for v in disposition_tally.values():
            self.assertIsInstance(v, int)
            self.assertGreater(v, 0)
        self.assertEqual(sum(disposition_tally.values()), d["count"])


class InvoicesTallyTest(_BaseSubprocessCase):
    """§S1 — `query invoices` (TOON) carries `tally.status` (sums to `count`)
    and `tally.acct` with the personal/business split — even though invoices'
    default TOON field-shaping (`DEFAULT_FIELDS['invoices']`) drops both
    `status` and `acct` from the printed rows, proving the tally is computed
    from the raw fetched docs, not the already-shaped `results`."""

    def setUp(self):
        super().setUp()
        self.db["invoices"].insert_many([dict(r) for r in INVOICES_SEED])

    def test_status_tally_sums_to_count_despite_shaped_rows_lacking_status(self):
        result = self._run(["query", "invoices"])
        self.assertEqual(result.returncode, 0, f"query failed: {result.stderr}")

        d = oa_toon.from_toon(result.stdout.strip())
        self.assertIsInstance(d, dict, f"expected an envelope dict, got {type(d).__name__}: {d!r}")
        self.assertEqual(d["count"], 4)
        self.assertIn("tally", d)

        tally = d["tally"]
        self.assertIn("status", tally)
        status_tally = tally["status"]
        # positive: exact per-status counts from the seeded mix (2xNEW, 1xIN_PROGRESS, 1xCOMPLETED)
        self.assertEqual(status_tally, {"NEW": 2, "IN_PROGRESS": 1, "COMPLETED": 1})
        self.assertEqual(sum(status_tally.values()), d["count"])
        self.assertNotIn("DUE", status_tally)
        self.assertNotIn("EXPIRED", status_tally)

    def test_acct_tally_present_with_personal_business_split(self):
        result = self._run(["query", "invoices"])
        self.assertEqual(result.returncode, 0, f"query failed: {result.stderr}")

        d = oa_toon.from_toon(result.stdout.strip())
        self.assertIsInstance(d, dict, f"expected an envelope dict, got {type(d).__name__}: {d!r}")
        self.assertIn("tally", d)
        tally = d["tally"]
        self.assertIn("acct", tally, "invoices carry 'acct' — tally must include the personal/business split")

        acct_tally = tally["acct"]
        # positive: exact split from the seeded mix (2 personal, 2 business)
        self.assertEqual(acct_tally, {"personal": 2, "business": 2})
        self.assertEqual(sum(acct_tally.values()), d["count"])


class EmptyQueryTallyTest(_BaseSubprocessCase):
    """§S1 — an empty-result TOON query shows `count:0` with an empty/all-zero
    `tally`, and exits 0 (no crash on an empty-results tally computation)."""

    def setUp(self):
        super().setUp()
        self.db["subscriptions"].insert_many([dict(r) for r in SUBSCRIPTIONS_SEED])

    def test_no_match_query_has_zero_count_and_empty_tally_no_crash(self):
        result = self._run(["query", "subscriptions", "--where", "provider=__none__"])
        self.assertEqual(
            result.returncode, 0,
            f"query on a non-matching filter must exit 0 (no crash), got rc={result.returncode}; "
            f"stderr: {result.stderr}",
        )

        d = oa_toon.from_toon(result.stdout.strip())
        self.assertIsInstance(d, dict, f"expected an envelope dict, got {type(d).__name__}: {d!r}")
        self.assertEqual(d["count"], 0)
        self.assertEqual(d["results"], [])
        self.assertIn("tally", d, "even an empty result must carry a 'tally' key")

        tally = d["tally"]
        self.assertIsInstance(tally, dict)
        # negative bound: every sub-map (if any) must be empty or sum to zero —
        # never a leftover positive count from an unrelated/previous query
        for sub_name, sub_map in tally.items():
            self.assertIsInstance(sub_map, dict, f"tally.{sub_name} must be a map, got {sub_map!r}")
            total = sum(v for v in sub_map.values() if isinstance(v, (int, float)))
            self.assertEqual(total, 0, f"tally.{sub_name} must be empty/all-zero for a 0-count query, got {sub_map!r}")

    def test_matching_query_is_not_mistaken_for_empty(self):
        # positive control: same shape of call, but a filter that DOES match —
        # tally must reflect the single match, not always be empty.
        result = self._run(["query", "subscriptions", "--where", "provider=Streamly"])
        self.assertEqual(result.returncode, 0, f"query failed: {result.stderr}")
        d = oa_toon.from_toon(result.stdout.strip())
        self.assertIsInstance(d, dict, f"expected an envelope dict, got {type(d).__name__}: {d!r}")
        self.assertEqual(d["count"], 1)
        self.assertIn("tally", d)
        self.assertEqual(d["tally"]["status"], {"IN_PROGRESS": 1})
        self.assertEqual(d["tally"]["disposition"], {"KEEP": 1})


class JsonContractStaysTallyFreeTest(_BaseSubprocessCase):
    """Contract (decision B) — `--json` / `VIDUSHI_FORMAT=json` stay a bare
    JSON array: no `tally` key anywhere, no envelope wrapper at all. These
    PASS today (the envelope doesn't exist yet on ANY path) and must stay
    green after GREEN adds `tally` to the TOON path only."""

    def setUp(self):
        super().setUp()
        self.db["invoices"].insert_many([dict(r) for r in INVOICES_SEED])

    def test_json_flag_bare_array_no_tally(self):
        result = self._run(["query", "invoices", "--json"])
        self.assertEqual(result.returncode, 0, f"query --json failed: {result.stderr}")

        parsed = json.loads(result.stdout)
        self.assertIsInstance(parsed, list, f"expected a bare JSON array, got {type(parsed).__name__}")
        self.assertNotIsInstance(parsed, dict)
        self.assertEqual(len(parsed), 4)
        self.assertFalse(
            _find_key_anywhere(parsed, "tally"),
            "the --json contract must never leak a 'tally' key at any depth",
        )

    def test_vidushi_format_json_env_bare_array_no_tally(self):
        result = self._run(["query", "invoices"], extra_env={"VIDUSHI_FORMAT": "json"})
        self.assertEqual(result.returncode, 0, f"VIDUSHI_FORMAT=json query failed: {result.stderr}")

        parsed = json.loads(result.stdout)
        self.assertIsInstance(parsed, list, f"expected a bare JSON array, got {type(parsed).__name__}")
        self.assertNotIsInstance(parsed, dict)
        self.assertEqual(len(parsed), 4)
        self.assertFalse(
            _find_key_anywhere(parsed, "tally"),
            "VIDUSHI_FORMAT=json must never leak a 'tally' key at any depth",
        )


if __name__ == "__main__":
    unittest.main()
