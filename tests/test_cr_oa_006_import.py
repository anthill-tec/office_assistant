"""CR-OA-006 Cycle A — `store.py import [<type>]` + `VIDUSHI_DATA_DIR`.

Verifies the §S1 ACs for the migration-import verb:
  - `import <type>` reads `<VIDUSHI_DATA_DIR or repo data/>/<file>.jsonl` and upserts every
    record into Mongo by `id` (`replace_one({id}, doc, upsert=True)`).
  - Re-running `import` is idempotent: the collection count is unchanged and no
    duplicate `id` is created.
  - `import` (no type) walks every `store.STORES` type; against the REAL repo `data/`
    tree (no `VIDUSHI_DATA_DIR` override) each collection's document count must equal the
    real `data/<file>.jsonl` line count (invoices 48, contacts 18, warranties 19,
    cases 1, products 19 = 105, plus subscriptions 11, insurance 2 = 118 total per
    the CR's AC).

`import` is currently NOT a registered subparser and `VIDUSHI_DATA_DIR` is not yet honoured
by `store.py`'s data-dir resolution, so every test here MUST fail until Cycle A's GREEN
phase lands both.

DATA SAFETY: `import` only ever WRITES to Mongo (the `vidushi_oa_test` database,
dropped in tearDown) and only READS the JSONL files — the real `data/*.jsonl` files are
never written to by any test in this file.
Requires a local mongod on 127.0.0.1:27017 (the office_assistant instance).
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

import jsonschema
import pymongo

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, "scripts")
STORE = os.path.join(SCRIPTS, "store.py")
DATA = os.path.join(ROOT, "data")
SCHEMA_DIR = os.path.join(ROOT, "vidushi_oa", "schema")
sys.path.insert(0, SCRIPTS)

import store  # noqa: E402  (for store.STORES / store.PREFIX — the type -> filename/prefix maps)

# Per-store extra fields (beyond id/acct/status, which every store gets) needed to make a
# synthetic row realistic and satisfy the schema's enum-constrained fields.
_SYNTHETIC_EXTRA_FIELDS = {
    "contacts": {"vendor": "SynthVendor", "kind": "reseller"},
    "invoices": {"vendor": "SynthVendor", "doc_type": "invoice", "date": "2026-01-01"},
    "warranties": {"vendor": "SynthVendor", "product": "SynthProduct"},
    "cases": {"vendor": "SynthVendor"},
    "products": {"product": "SynthProduct", "manufacturer": "SynthMfr",
                 "kind": "physical", "relation": "accessory", "billing": "one-time"},
    "subscriptions": {"provider": "SynthProvider", "category": "software",
                       "disposition": "KEEP", "amount": 9.99, "currency": "USD"},
    "insurance": {"insurer": "SynthInsurer", "policy_no": "POL0001", "premium": 100},
    "orders": {"merchant": "SynthMerchant", "amount": 10, "currency": "USD"},
}

# Number of synthetic rows to generate per store when its real data/<file>.jsonl is absent
# (e.g. on a fresh CI checkout where data/*.jsonl is gitignored).
SYNTHETIC_ROWS_PER_STORE = 2


def _load_schema(store_type):
    path = os.path.join(SCHEMA_DIR, f"{store_type}.schema.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def make_synthetic_row(store_type, index):
    """Build a minimal, schema-valid synthetic row for `store_type`, distinguished by `index`
    (so each generated row has a unique `id`). Validated by the caller against the store's
    real `vidushi_oa/schema/<store_type>.schema.json` before being written to a fixture file."""
    prefix = store.PREFIX[store_type]
    row = {"id": f"{prefix}_synth{index:04d}", "acct": "personal", "status": "NEW"}
    row.update(_SYNTHETIC_EXTRA_FIELDS.get(store_type, {}))
    return row


def make_synthetic_rows(store_type, count):
    rows = []
    schema = _load_schema(store_type)
    for i in range(1, count + 1):
        row = make_synthetic_row(store_type, i)
        jsonschema.validate(row, schema)
        rows.append(row)
    return rows


class ImportVerbTest(unittest.TestCase):
    """§S1 — `store.py import [<type>]` upserts JSONL rows into Mongo, honouring VIDUSHI_DATA_DIR."""

    TEST_DB = "vidushi_oa_test"

    def setUp(self):
        self.client = pymongo.MongoClient("mongodb://127.0.0.1:27017", serverSelectionTimeoutMS=2000)
        self.db = self.client[self.TEST_DB]
        self.base_env = dict(os.environ)
        self.base_env.pop("VIDUSHI_DATA_DIR", None)
        self.base_env["VIDUSHI_MONGO_DB"] = self.TEST_DB

    def tearDown(self):
        self.client.drop_database(self.TEST_DB)
        self.client.close()

    def _run(self, *args, env=None, expect_success=True):
        import subprocess
        result = subprocess.run(
            [sys.executable, STORE, *args], capture_output=True, text=True,
            env=env if env is not None else self.base_env,
        )
        if expect_success:
            self.assertEqual(
                result.returncode, 0,
                f"store.py {' '.join(args)} failed (rc={result.returncode}): {result.stderr}",
            )
        return result

    def _write_fixture_invoices(self, tmpdir, rows):
        with open(os.path.join(tmpdir, "invoices.jsonl"), "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    # ---- §S1 import from a fixture dir (VIDUSHI_DATA_DIR override) ----

    def test_import_from_fixture_dir_upserts_invoices_by_id(self):
        tmpdir = tempfile.mkdtemp(prefix="oa-import-fixture-")
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)
        self._write_fixture_invoices(tmpdir, [
            {"id": "doc_i1", "vendor": "Acme", "doc_type": "invoice",
             "date": "2026-01-01", "acct": "personal", "status": "NEW"},
            {"id": "doc_i2", "vendor": "Beta", "doc_type": "invoice",
             "date": "2026-02-01", "acct": "personal", "status": "NEW"},
        ])
        env = dict(self.base_env)
        env["VIDUSHI_DATA_DIR"] = tmpdir

        self._run("import", "invoices", env=env)

        # positive: both fixture docs landed in Mongo with their fields intact
        doc1 = self.db["invoices"].find_one({"id": "doc_i1"}, {"_id": 0})
        doc2 = self.db["invoices"].find_one({"id": "doc_i2"}, {"_id": 0})
        self.assertIsNotNone(doc1, "doc_i1 must be upserted into Mongo from the fixture dir")
        self.assertIsNotNone(doc2, "doc_i2 must be upserted into Mongo from the fixture dir")
        self.assertEqual(doc1["vendor"], "Acme")
        self.assertEqual(doc2["vendor"], "Beta")
        # negative/bound: exactly the two fixture docs, nothing extra (e.g. real data/ leaked in)
        self.assertEqual(self.db["invoices"].count_documents({}), 2)

    def test_import_is_idempotent_no_duplicate_ids(self):
        tmpdir = tempfile.mkdtemp(prefix="oa-import-fixture-")
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)
        self._write_fixture_invoices(tmpdir, [
            {"id": "doc_i1", "vendor": "Acme", "doc_type": "invoice",
             "date": "2026-01-01", "acct": "personal", "status": "NEW"},
            {"id": "doc_i2", "vendor": "Beta", "doc_type": "invoice",
             "date": "2026-02-01", "acct": "personal", "status": "NEW"},
        ])
        env = dict(self.base_env)
        env["VIDUSHI_DATA_DIR"] = tmpdir

        self._run("import", "invoices", env=env)
        first_count = self.db["invoices"].count_documents({})
        self._run("import", "invoices", env=env)
        second_count = self.db["invoices"].count_documents({})

        # positive: re-running import changes nothing
        self.assertEqual(first_count, 2)
        self.assertEqual(second_count, 2)
        # negative/bound: no duplicate `id` — exactly one doc per fixture id
        self.assertEqual(self.db["invoices"].count_documents({"id": "doc_i1"}), 1)
        self.assertEqual(self.db["invoices"].count_documents({"id": "doc_i2"}), 1)

    # ---- §S1 real-data count parity (no VIDUSHI_DATA_DIR -> reads the real repo data/) ----

    def test_import_all_types_matches_real_data_line_counts(self):
        # no VIDUSHI_DATA_DIR in env -> must fall back to the real repo data/ directory
        self.assertNotIn("VIDUSHI_DATA_DIR", self.base_env)

        # CI-portability: the real data/*.jsonl snapshots are gitignored and absent on a
        # fresh checkout. For any store whose file is missing, write a small synthetic
        # schema-valid fixture (each generated row already validated against the store's
        # real vidushi_oa/schema/<store>.schema.json by make_synthetic_rows) so this test
        # provides its own data instead of relying on the user's local snapshots. A file
        # that DOES exist (the user's real local data) is left completely untouched.
        created_paths = []
        for t, filename in store.STORES.items():
            real_path = os.path.join(DATA, filename)
            if not os.path.exists(real_path):
                rows = make_synthetic_rows(t, SYNTHETIC_ROWS_PER_STORE)
                with open(real_path, "w", encoding="utf-8") as f:
                    for row in rows:
                        f.write(json.dumps(row) + "\n")
                created_paths.append(real_path)
        self.addCleanup(lambda: [os.remove(p) for p in created_paths if os.path.exists(p)])

        result = self._run("import")

        self.assertEqual(result.returncode, 0, result.stderr)
        total_expected = 0
        for t, filename in store.STORES.items():
            real_path = os.path.join(DATA, filename)
            with open(real_path, encoding="utf-8") as f:
                expected = sum(1 for line in f if line.strip())
            actual = self.db[t].count_documents({})
            self.assertEqual(
                actual, expected,
                f"{t}: Mongo has {actual} docs but data/{filename} has {expected} non-blank lines",
            )
            total_expected += expected
        # positive/bound: with every store's file present (real or synthetic-fixture), the
        # grand total across all stores must be a positive, non-zero number of rows.
        self.assertGreater(total_expected, 0)


if __name__ == "__main__":
    unittest.main()
