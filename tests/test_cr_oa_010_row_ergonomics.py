"""CR-OA-010 Cycle A — AXI ergonomics #2 (minimal default schemas) + #3
(content truncation), gated behind a shared `--full` flag. TOON-only (decision
"B"): the `--json` / `OA_FORMAT=json` contract stays a clean, full-data array,
byte-stable and untruncated.

Verifies:
  §S1 — in TOON with NO `--fields` and NO `--full`, `query <type>` projects
        rows down to a per-store `DEFAULT_FIELDS` map (~3-4 identifying
        fields); `--full` restores every field; `--fields` still overrides
        the default (existing pre-CR behavior, must keep working).
  §S2 — a long string value in a DEFAULT field truncates to a ~80-char cap
        with a `<prefix>…(+N chars)` hint in TOON; `--full` disables
        truncation.
  contract — `--json` (and `OA_FORMAT=json`) is untouched: full fields,
        untruncated values, bare JSON array (no envelope).

None of this exists in `store.py` yet — `project()` only honors `--fields`
and does no default-projection or truncation, so tests 1-4 (and the
subscriptions default-fields test) MUST fail today; the JSON contract tests
(5) pass today and continue to guard decision "B" post-GREEN.

DATA SAFETY: every subprocess call points `OA_DATA_DIR` at an EMPTY tempdir
(never the real repo `data/`) and `OA_MONGO_DB` at `office_assistant_test`
(never the real DB), dropped in tearDown. Requires a local mongod on
127.0.0.1:27017 (the office_assistant instance; CR-OA-001).
"""
import json
import os
import re
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

# The DEFAULT_FIELDS contract this CR defines — GREEN's store.py DEFAULT_FIELDS
# map must match these exactly for these two stores.
DEFAULT_FIELDS_PRODUCTS = ["id", "product", "manufacturer", "category"]
DEFAULT_FIELDS_SUBSCRIPTIONS = ["id", "provider", "disposition", "status", "renews"]

HEADER_RE = re.compile(r"^\[\d+[,]?\]\{([^}]*)\}:")
TRUNC_RE = re.compile(r"^(.{1,80})…\(\+(\d+) chars\)$")

# A DEFAULT field (category) padded well past the ~80 char cap.
LONG_CATEGORY = "consumer-electronics-" + ("x" * 130)

PRODUCT_RICH = {
    "id": "prod_zoom_h1",
    "product": "H1 Handy Recorder",
    "manufacturer": "Zoom",
    "category": "audio-recorder",
    "acct": "personal",
    "kind": "physical",
    "links": {"manual": "https://zoomcorp.com/h1/manual", "spec": "https://zoomcorp.com/h1/spec"},
    "notes": "Bought for podcast episodes; SD card slot occasionally flaky under heavy use.",
    "source": {"email_id": "msg-abc123", "mailbox": "fastmail"},
    "key_specs": "16-bit/44.1kHz WAV, built-in X/Y stereo mic, onboard speaker, USB audio interface",
}
PRODUCT_PLAIN = {
    "id": "prod_acme_widget",
    "product": "Widget",
    "manufacturer": "Acme",
    "category": "gadget",
    "acct": "personal",
}
PRODUCT_LONG_CATEGORY = {
    "id": "prod_longco_gizmo",
    "product": "Gizmo",
    "manufacturer": "LongCo",
    "category": LONG_CATEGORY,
    "acct": "personal",
}
SUBSCRIPTION_RICH = {
    "id": "sub_streamly",
    "provider": "Streamly",
    "category": "streaming",
    "disposition": "KEEP",
    "plan": "Premium 4K",
    "cadence": "monthly",
    "amount": 499,
    "currency": "INR",
    "alias": "streamly-personal",
    "status": "IN_PROGRESS",
    "renews": "2026-08-01",
}


def _header_fields(stdout):
    """Extract the ordered field-name list from a TOON tabular-array header
    line (`[N,]{f1,f2,...}:`), skipping any blank lines."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    assert lines, f"no output produced: {stdout!r}"
    m = HEADER_RE.match(lines[0])
    assert m, f"first non-blank line is not a TOON tabular header: {lines[0]!r}"
    return [f for f in m.group(1).split(",") if f]


class _BaseSubprocessCase(unittest.TestCase):
    """Shared Mongo + tempdir isolation harness for CLI subprocess tests."""

    def setUp(self):
        self.data_dir = tempfile.mkdtemp(prefix="oa-cr010a-")
        self.env = dict(os.environ)
        self.env["OA_MONGO_DB"] = TEST_DB
        self.env["OA_DATA_DIR"] = self.data_dir
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


class ProductsDefaultFieldsTest(_BaseSubprocessCase):
    """§S1 — TOON `query products` with no flags projects to DEFAULT_FIELDS;
    `--full` restores everything; `--fields` still overrides."""

    def setUp(self):
        super().setUp()
        self.db["products"].insert_many([dict(PRODUCT_RICH), dict(PRODUCT_PLAIN)])

    def test_default_toon_query_shows_only_default_fields(self):
        result = self._run(["query", "products"])
        self.assertEqual(result.returncode, 0, f"query failed: {result.stderr}")

        fields = _header_fields(result.stdout)
        self.assertEqual(set(fields), set(DEFAULT_FIELDS_PRODUCTS))
        self.assertEqual(len(fields), len(DEFAULT_FIELDS_PRODUCTS))
        for leaked in ("links", "notes", "source", "key_specs", "acct", "kind"):
            self.assertNotIn(leaked, fields, f"{leaked!r} must not appear in the default TOON projection")

        decoded = oa_toon.from_toon(result.stdout.strip())
        self.assertEqual(len(decoded), 2)
        for row in decoded:
            self.assertEqual(set(row.keys()), set(DEFAULT_FIELDS_PRODUCTS))
        # negative bound: exactly the two seeded ids, nothing dropped/extra
        self.assertEqual(sorted(r["id"] for r in decoded), ["prod_acme_widget", "prod_zoom_h1"])

    def test_full_flag_restores_every_field(self):
        result = self._run(["query", "products", "--full"])
        self.assertEqual(result.returncode, 0, f"query --full failed: {result.stderr}")

        # NOTE: --full shows raw docs with nested-dict values (links/source),
        # so the `toon` encoder legitimately falls back to non-tabular list
        # form ([N]: rather than [N,]{f1,f2,...}:) — decode structurally
        # instead of asserting a tabular header (which only applies to
        # uniform-primitive rows, e.g. the projected/truncated default view).
        decoded = oa_toon.from_toon(result.stdout.strip())
        self.assertEqual(len(decoded), 2)

        all_fields = set().union(*(r.keys() for r in decoded))
        rich_keys = set(PRODUCT_RICH.keys())
        self.assertTrue(rich_keys.issubset(all_fields), f"missing under --full: {rich_keys - all_fields}")
        for f in DEFAULT_FIELDS_PRODUCTS:
            self.assertIn(f, all_fields, f"--full must still include the default field {f!r}")
        # negative bound: --full is NOT filtered down to just the default subset
        self.assertIn("notes", all_fields)
        self.assertIn("links", all_fields)

        rich_row = next(r for r in decoded if r["id"] == "prod_zoom_h1")
        self.assertEqual(rich_row["notes"], PRODUCT_RICH["notes"])
        self.assertEqual(rich_row["links"], PRODUCT_RICH["links"])
        # negative bound: --full must not still be filtered down to the default subset
        self.assertNotEqual(set(rich_row.keys()), set(DEFAULT_FIELDS_PRODUCTS))

    def test_fields_flag_overrides_default(self):
        result = self._run(["query", "products", "--fields", "id,category"])
        self.assertEqual(result.returncode, 0, f"query --fields failed: {result.stderr}")

        fields = _header_fields(result.stdout)
        self.assertEqual(set(fields), {"id", "category"})
        self.assertEqual(len(fields), 2)

        decoded = oa_toon.from_toon(result.stdout.strip())
        for row in decoded:
            self.assertEqual(set(row.keys()), {"id", "category"})


class SubscriptionsDefaultFieldsTest(_BaseSubprocessCase):
    """§S1 — the DEFAULT_FIELDS contract also holds for a second store
    (subscriptions), proving the map is per-store, not products-only."""

    def setUp(self):
        super().setUp()
        self.db["subscriptions"].insert_one(dict(SUBSCRIPTION_RICH))

    def test_default_toon_query_shows_only_default_fields(self):
        result = self._run(["query", "subscriptions"])
        self.assertEqual(result.returncode, 0, f"query failed: {result.stderr}")

        fields = _header_fields(result.stdout)
        self.assertEqual(set(fields), set(DEFAULT_FIELDS_SUBSCRIPTIONS))
        self.assertEqual(len(fields), len(DEFAULT_FIELDS_SUBSCRIPTIONS))
        for leaked in ("plan", "cadence", "amount", "currency", "alias", "category"):
            self.assertNotIn(leaked, fields, f"{leaked!r} must not appear in the default TOON projection")

        decoded = oa_toon.from_toon(result.stdout.strip())
        self.assertEqual(len(decoded), 1)
        self.assertEqual(set(decoded[0].keys()), set(DEFAULT_FIELDS_SUBSCRIPTIONS))
        self.assertEqual(decoded[0]["id"], "sub_streamly")


class ProductsTruncationTest(_BaseSubprocessCase):
    """§S2 — a long value in a DEFAULT field truncates with a size hint in
    TOON by default; `--full` disables truncation."""

    def setUp(self):
        super().setUp()
        self.db["products"].insert_many([dict(PRODUCT_LONG_CATEGORY), dict(PRODUCT_PLAIN)])

    def test_default_toon_truncates_long_default_field_value(self):
        result = self._run(["query", "products"])
        self.assertEqual(result.returncode, 0, f"query failed: {result.stderr}")

        decoded = oa_toon.from_toon(result.stdout.strip())
        row = next(r for r in decoded if r["id"] == "prod_longco_gizmo")
        cat = row["category"]

        self.assertNotEqual(cat, LONG_CATEGORY, "long category value must be truncated in default TOON output")
        m = TRUNC_RE.match(cat)
        self.assertIsNotNone(m, f"truncated value missing the <prefix>…(+N chars) hint: {cat!r}")
        prefix, extra = m.group(1), int(m.group(2))
        self.assertEqual(prefix, LONG_CATEGORY[:80])
        self.assertEqual(extra, len(LONG_CATEGORY) - 80)

        # negative bound: the OTHER (short) product's default field is untouched
        plain_row = next(r for r in decoded if r["id"] == "prod_acme_widget")
        self.assertEqual(plain_row["category"], PRODUCT_PLAIN["category"])
        self.assertNotIn("…(+", plain_row["category"])

    def test_full_flag_disables_truncation(self):
        result = self._run(["query", "products", "--full"])
        self.assertEqual(result.returncode, 0, f"query --full failed: {result.stderr}")

        decoded = oa_toon.from_toon(result.stdout.strip())
        row = next(r for r in decoded if r["id"] == "prod_longco_gizmo")
        self.assertEqual(row["category"], LONG_CATEGORY)
        self.assertNotIn("…(+", row["category"])
        # negative bound: full length preserved exactly, not just "longer than before"
        self.assertEqual(len(row["category"]), len(LONG_CATEGORY))


class JsonContractUnchangedTest(_BaseSubprocessCase):
    """Contract — `--json` / `OA_FORMAT=json` stay a clean, full-data,
    untruncated bare array (decision "B"). These PASS today; they guard the
    fork so GREEN cannot accidentally leak the TOON reshaping into JSON."""

    def setUp(self):
        super().setUp()
        self.db["products"].insert_many([dict(PRODUCT_RICH), dict(PRODUCT_LONG_CATEGORY)])

    def test_bare_json_flag_returns_full_untruncated_documents(self):
        result = self._run(["query", "products", "--json"])
        self.assertEqual(result.returncode, 0, f"query --json failed: {result.stderr}")

        parsed = json.loads(result.stdout)
        self.assertIsInstance(parsed, list)
        self.assertNotIsInstance(parsed, dict)  # negative: no envelope wrapper
        self.assertEqual(len(parsed), 2)

        rich = next(r for r in parsed if r["id"] == "prod_zoom_h1")
        self.assertIn("links", rich)
        self.assertIn("notes", rich)
        self.assertEqual(rich["notes"], PRODUCT_RICH["notes"])
        self.assertEqual(rich["links"], PRODUCT_RICH["links"])

        long_row = next(r for r in parsed if r["id"] == "prod_longco_gizmo")
        self.assertEqual(long_row["category"], LONG_CATEGORY)
        self.assertNotIn("…(+", long_row["category"])

    def test_oa_format_json_env_returns_full_untruncated_documents(self):
        result = self._run(["query", "products"], extra_env={"OA_FORMAT": "json"})
        self.assertEqual(result.returncode, 0, f"OA_FORMAT=json query failed: {result.stderr}")

        parsed = json.loads(result.stdout)
        self.assertIsInstance(parsed, list)
        self.assertEqual(len(parsed), 2)

        rich = next(r for r in parsed if r["id"] == "prod_zoom_h1")
        self.assertIn("links", rich)
        self.assertEqual(rich["links"], PRODUCT_RICH["links"])

        long_row = next(r for r in parsed if r["id"] == "prod_longco_gizmo")
        self.assertEqual(long_row["category"], LONG_CATEGORY)
        self.assertNotIn("…(+", long_row["category"])
        # negative bound: exactly the seeded two, nothing extra
        self.assertEqual(sorted(r["id"] for r in parsed), ["prod_longco_gizmo", "prod_zoom_h1"])


if __name__ == "__main__":
    unittest.main()
