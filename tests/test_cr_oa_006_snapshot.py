"""CR-OA-006 Cycle B — `store.py snapshot [<type>]`.

Verifies the §S2 ACs for the versioning-export verb:
  - `snapshot <type>` exports each Mongo collection -> `<OA_DATA_DIR>/<file>.jsonl`
    (per `store.STORES`), one JSON object per line, with the Mongo `_id` stripped.
  - Keys are emitted in a STABLE order: `id` first, then the rest sorted
    (per the CR's "id first then sorted" wording) -> byte-identical output across
    repeated runs (no key-order churn / noisy diffs).
  - Round-trip fidelity: every seeded `(id, status)` pair survives the export with
    no field loss and no stray `_id`.

`snapshot` is currently NOT a registered subparser, so every test here MUST fail
(non-zero exit / "invalid choice") until Cycle B's GREEN phase lands it.

DATA SAFETY: every `store.py` subprocess call in this file sets `OA_DATA_DIR` to a
throwaway `tempfile.mkdtemp()` directory (removed in tearDown) and `OA_MONGO_DB` to
the `office_assistant_test` database (dropped in tearDown) — `snapshot` never
writes to the real repo `data/` tree or the real Mongo database.
Requires a local mongod on 127.0.0.1:27017 (the office_assistant instance).
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

import store  # noqa: E402  (for store.STORES — the type -> filename map)


class SnapshotVerbTest(unittest.TestCase):
    """§S2 — `store.py snapshot [<type>]` exports Mongo -> `<OA_DATA_DIR>/<file>.jsonl`."""

    TEST_DB = "office_assistant_test"

    def setUp(self):
        self.client = pymongo.MongoClient("mongodb://127.0.0.1:27017", serverSelectionTimeoutMS=2000)
        self.db = self.client[self.TEST_DB]
        self.tmpdir = tempfile.mkdtemp(prefix="oa-snapshot-")
        self.env = dict(os.environ)
        self.env["OA_MONGO_DB"] = self.TEST_DB
        self.env["OA_DATA_DIR"] = self.tmpdir
        self.env["OA_FORMAT"] = "json"

    def tearDown(self):
        self.client.drop_database(self.TEST_DB)
        self.client.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

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

    def _seed_invoices(self, rows):
        # pymongo auto-assigns an ObjectId `_id` on insert — snapshot must strip it back out.
        self.db["invoices"].insert_many([dict(r) for r in rows])

    def _snapshot_path(self, t="invoices"):
        return os.path.join(self.tmpdir, store.STORES[t])

    def _read_lines(self, path):
        with open(path, encoding="utf-8") as f:
            return [line for line in f if line.strip()]

    # ---- §S2 export, `_id` stripped ----

    def test_snapshot_exports_invoices_with_id_stripped(self):
        self._seed_invoices([
            {"id": "doc_s1", "vendor": "X", "status": "COMPLETED"},
            {"id": "doc_s2", "vendor": "Y", "status": "NEW"},
        ])

        self._run("snapshot", "invoices")

        out_path = self._snapshot_path("invoices")
        self.assertTrue(os.path.exists(out_path), f"{out_path} must be created by snapshot")
        lines = self._read_lines(out_path)
        # positive/bound: exactly the two seeded docs, nothing extra
        self.assertEqual(len(lines), 2)
        seen_ids = set()
        for line in lines:
            rec = json.loads(line)  # each line must be valid, self-contained JSON
            self.assertNotIn("_id", rec, "snapshot must strip the Mongo _id field")
            self.assertIn(rec["id"], {"doc_s1", "doc_s2"})
            seen_ids.add(rec["id"])
        self.assertEqual(seen_ids, {"doc_s1", "doc_s2"})

    # ---- §S2 stable / deterministic key order across runs ----

    def test_snapshot_is_stable_and_id_first_across_runs(self):
        self._seed_invoices([
            {"id": "doc_s1", "vendor": "X", "status": "COMPLETED"},
            {"id": "doc_s2", "vendor": "Y", "status": "NEW"},
        ])

        self._run("snapshot", "invoices")
        out_path = self._snapshot_path("invoices")
        with open(out_path, "rb") as f:
            first_bytes = f.read()

        self._run("snapshot", "invoices")
        with open(out_path, "rb") as f:
            second_bytes = f.read()

        # positive: re-running snapshot against the same Mongo state is byte-identical
        # (no key-order churn -> no noisy `git diff`)
        self.assertEqual(first_bytes, second_bytes)

        # positive: each line's key order is `id` first, then the rest sorted
        for line in self._read_lines(out_path):
            keys = list(json.loads(line).keys())
            self.assertEqual(keys[0], "id", f"first key must be 'id', got order {keys}")
            self.assertEqual(
                keys[1:], sorted(keys[1:]),
                f"keys after 'id' must be sorted, got order {keys}",
            )

    # ---- §S3 round-trip fidelity (no field loss, no _id added) ----

    def test_snapshot_round_trip_matches_seeded_id_status_pairs(self):
        seeded = [
            {"id": "doc_s1", "vendor": "X", "status": "COMPLETED"},
            {"id": "doc_s2", "vendor": "Y", "status": "NEW"},
        ]
        self._seed_invoices(seeded)
        expected_pairs = {(r["id"], r["status"]) for r in seeded}

        self._run("snapshot", "invoices")

        out_path = self._snapshot_path("invoices")
        actual_pairs = set()
        for line in self._read_lines(out_path):
            rec = json.loads(line)
            self.assertNotIn("_id", rec, "round-tripped record must not carry a stray _id")
            actual_pairs.add((rec["id"], rec["status"]))

        # positive: exact set match — no field loss, no record added or dropped
        self.assertEqual(actual_pairs, expected_pairs)
        # negative/bound: exactly 2 pairs, not a superset/subset
        self.assertEqual(len(actual_pairs), 2)


if __name__ == "__main__":
    unittest.main()
