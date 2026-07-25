"""CR-OA-018 §S4 — Mongo -> SQLite migration, field-level fidelity (RED/characterization).

Drives the REAL `voa` CLI (via `scripts/store.py`, subprocess) to perform an actual
Mongo -> `data/*.jsonl` -> SQLite cutover and checks the §S4 acceptance criteria:

  1. Field-level fidelity: a `cases` row with nested `actions[]` (opened + resolved),
     an `--append-log` case-log entry, and a `documents[]` entry, plus an `orders` row
     with nested `actions[]`, survive the migration byte-for-byte (deep-equal per id).
  2. Per-store row counts on SQLite match Mongo, and `validate` is clean (`[]`) on SQLite.
  3. The cutover leaves Mongo INTACT (no live data dropped; rollback via re-`import`
     under `VIDUSHI_BACKEND=mongo` stays available).
  4. A SQLite `snapshot` -> re-`import` round-trip is idempotent (counts + a spot-checked
     record unchanged).

NOTE (dispatch): `snapshot`/`import`/`get`/`stats`/`validate` were just refactored to be
backend-agnostic (they all resolve their store via `get_backend()`), so SOME assertions
here MAY already pass against the current code — that is expected for a migration
characterization pass. Each test's docstring/comments call out what it is actually
checking so a RED failure vs an already-GREEN pass can both be read directly off the
test names in the pytest report.

DATA SAFETY: `VIDUSHI_DATA_DIR` points at a throwaway tempdir (never the real repo
`data/`), `VIDUSHI_MONGO_DB` at `vidushi_oa_test` (never the real `vidushi_oa` DB,
dropped in tearDownClass), and `VIDUSHI_SQLITE_PATH` at a throwaway tempfile (removed in
tearDownClass). `os.environ` is snapshotted/restored around the whole class. Requires a
local mongod on 127.0.0.1:27017 (the office_assistant instance, CR-OA-001) and the
`jsonschema` package (the sqlite backend's write-validation dependency).
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

import store  # noqa: E402 -- for store.STORES, the type -> jsonl-filename map

TEST_DB = "vidushi_oa_test"
_ENV_KEYS = ("VIDUSHI_BACKEND", "VIDUSHI_DATA_DIR", "VIDUSHI_MONGO_DB",
             "VIDUSHI_SQLITE_PATH", "VIDUSHI_FORMAT")


def _run(backend, *args, expect_success=True):
    """Run `scripts/store.py <args>` with VIDUSHI_BACKEND=<backend>, inheriting the rest
    of the migration env from os.environ (set by setUpClass). Returns the CompletedProcess;
    asserts rc==0 by default (with stdout/stderr in the message) unless expect_success=False."""
    os.environ["VIDUSHI_BACKEND"] = backend
    result = subprocess.run([sys.executable, STORE, *args], capture_output=True, text=True)
    if expect_success:
        assert result.returncode == 0, (
            f"[{backend}] store.py {' '.join(args)} failed (rc={result.returncode}): "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
    return result


def _get(backend, type_, id_):
    result = _run(backend, "get", type_, id_, "--json", "--full")
    return json.loads(result.stdout.strip())


def _stats_total(backend, type_):
    result = _run(backend, "stats", type_)
    return json.loads(result.stdout.strip())["total"]


def _validate(backend, type_):
    result = _run(backend, "validate", type_)
    return json.loads(result.stdout.strip())


class MongoToSqliteMigrationFidelityTest(unittest.TestCase):
    """§S4 -- seeds RICH rows into Mongo, migrates them to SQLite via the real
    `snapshot` -> `import` path, and checks fidelity/counts/validator-cleanliness/
    Mongo-intactness/round-trip idempotency."""

    @classmethod
    def setUpClass(cls):
        cls._saved_env = {k: os.environ.get(k) for k in _ENV_KEYS}
        cls.data_dir = tempfile.mkdtemp(prefix="oa-cr018-migration-data-")
        cls.sqlite_dir = tempfile.mkdtemp(prefix="oa-cr018-migration-sqlite-")
        cls.sqlite_path = os.path.join(cls.sqlite_dir, "oa.db")

        os.environ["VIDUSHI_DATA_DIR"] = cls.data_dir
        os.environ["VIDUSHI_MONGO_DB"] = TEST_DB
        os.environ["VIDUSHI_SQLITE_PATH"] = cls.sqlite_path
        os.environ["VIDUSHI_FORMAT"] = "json"

        cls.client = pymongo.MongoClient("mongodb://127.0.0.1:27017", serverSelectionTimeoutMS=2000)
        cls.db = cls.client[TEST_DB]

        # ---- provision the Mongo side (validators + unique id indexes) ----
        _run("mongo", "init")

        # ---- seed a RICH `cases` row: nested actions[] (opened+resolved), an
        #      --append-log case-log entry, and a documents[] entry ----
        add_case = _run("mongo", "add", "cases", "--json", json.dumps({
            "vendor": "Migratron", "acct": "personal", "issue": "cracked screen",
        }))
        cls.case_id = json.loads(add_case.stdout.strip())["added"][0]
        _run("mongo", "action-add", "cases", cls.case_id, "raise-ticket", "--owner", "user")
        _run("mongo", "action-add", "cases", cls.case_id, "ship-back", "--owner", "user")
        _run("mongo", "action-resolve", "cases", cls.case_id, "raise-ticket")
        _run("mongo", "doc-add", "cases", cls.case_id, "ticket",
             "documents/personal/migratron/ticket.pdf", "--number", "TCK-1")
        _run("mongo", "update", "cases", cls.case_id,
             "--append-log", "called support, awaiting RMA")

        # ---- seed a rich `orders` row with a nested actions[] ----
        add_order = _run("mongo", "add", "orders", "--json", json.dumps({
            "merchant": "Migratron", "number": "O-1", "acct": "personal",
            "amount": 42.5, "currency": "INR",
        }))
        cls.order_id = json.loads(add_order.stdout.strip())["added"][0]
        _run("mongo", "action-add", "orders", cls.order_id, "shipment", "--owner", "agent")
        _run("mongo", "action-add", "orders", cls.order_id, "delivery", "--owner", "agent")
        _run("mongo", "action-resolve", "orders", cls.order_id, "shipment")

        # snapshot the BEFORE-cutover Mongo state, for the "left intact" assertion.
        cls.mongo_case_before = _get("mongo", "cases", cls.case_id)
        cls.mongo_order_before = _get("mongo", "orders", cls.order_id)
        cls.mongo_totals_before = {t: _stats_total("mongo", t) for t in store.STORES}

        # ---- the actual migration path under test ----
        _run("mongo", "snapshot")
        _run("sqlite", "init")
        _run("sqlite", "import")

    @classmethod
    def tearDownClass(cls):
        cls.client.drop_database(TEST_DB)
        cls.client.close()
        shutil.rmtree(cls.data_dir, ignore_errors=True)
        shutil.rmtree(cls.sqlite_dir, ignore_errors=True)
        for k, v in cls._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # ---- §S4 AC1: field-level fidelity ----

    def test_case_row_deep_equal_mongo_to_sqlite_after_migration(self):
        mongo_doc = _get("mongo", "cases", self.case_id)
        sqlite_doc = _get("sqlite", "cases", self.case_id)

        # positive: the FULL record, not just a subset of fields, deep-equals the source
        self.assertEqual(sqlite_doc, mongo_doc)

        # positive: nested actions[] history (opened/resolved) survived the round-trip
        actions_by_slug = {a["action"]: a for a in sqlite_doc["actions"]}
        self.assertEqual(actions_by_slug["raise-ticket"]["status"], "RESOLVED")
        self.assertIn("opened", actions_by_slug["raise-ticket"])
        self.assertIn("resolved", actions_by_slug["raise-ticket"])
        # negative bound: the OTHER action stayed OPEN (not silently flipped/dropped)
        self.assertEqual(actions_by_slug["ship-back"]["status"], "OPEN")
        self.assertNotIn("resolved", actions_by_slug["ship-back"])
        self.assertEqual(len(sqlite_doc["actions"]), 2)

        # positive: the --append-log case-log entry survived
        self.assertEqual(len(sqlite_doc.get("log", [])), 1)
        self.assertEqual(sqlite_doc["log"][0]["note"], "called support, awaiting RMA")

        # positive: the documents[] entry survived
        self.assertEqual(len(sqlite_doc.get("documents", [])), 1)
        self.assertEqual(sqlite_doc["documents"][0]["type"], "ticket")
        self.assertEqual(sqlite_doc["documents"][0]["number"], "TCK-1")
        self.assertEqual(sqlite_doc["documents"][0]["path"],
                          "documents/personal/migratron/ticket.pdf")

    def test_order_row_deep_equal_mongo_to_sqlite_after_migration(self):
        mongo_doc = _get("mongo", "orders", self.order_id)
        sqlite_doc = _get("sqlite", "orders", self.order_id)

        # positive: the FULL record deep-equals the source, including FKs/lifecycle fields
        self.assertEqual(sqlite_doc, mongo_doc)

        actions_by_slug = {a["action"]: a for a in sqlite_doc["actions"]}
        self.assertEqual(actions_by_slug["shipment"]["status"], "RESOLVED")
        self.assertIn("resolved", actions_by_slug["shipment"])
        # negative bound: the OTHER action stayed OPEN
        self.assertEqual(actions_by_slug["delivery"]["status"], "OPEN")
        self.assertEqual(len(sqlite_doc["actions"]), 2)

        self.assertEqual(sqlite_doc["merchant"], "Migratron")
        self.assertEqual(sqlite_doc["number"], "O-1")
        self.assertEqual(sqlite_doc["amount"], 42.5)
        self.assertEqual(sqlite_doc["currency"], "INR")
        self.assertEqual(sqlite_doc["acct"], "personal")

    # ---- §S4 AC2: counts + validator-clean on SQLite ----

    def test_sqlite_row_counts_match_mongo_totals_per_store(self):
        for t in store.STORES:
            mongo_total = _stats_total("mongo", t)
            sqlite_total = _stats_total("sqlite", t)
            self.assertEqual(sqlite_total, mongo_total,
                              f"{t}: sqlite total ({sqlite_total}) != mongo total ({mongo_total})")
        # positive/bound: the seeded stores are non-trivially populated, not 0==0 everywhere
        self.assertEqual(_stats_total("sqlite", "cases"), 1)
        self.assertEqual(_stats_total("sqlite", "orders"), 1)

    def test_sqlite_validate_returns_empty_for_every_store(self):
        for t in store.STORES:
            nonconforming = _validate("sqlite", t)
            self.assertEqual(nonconforming, [],
                              f"{t}: sqlite validate found nonconforming ids: {nonconforming}")

    # ---- §S4 AC3: cutover leaves Mongo intact ----

    def test_mongo_totals_unchanged_after_cutover(self):
        for t in store.STORES:
            after_total = _stats_total("mongo", t)
            self.assertEqual(after_total, self.mongo_totals_before[t],
                              f"{t}: mongo total changed after migration "
                              f"({self.mongo_totals_before[t]} -> {after_total})")

    def test_mongo_seeded_rows_unchanged_after_cutover(self):
        mongo_case_after = _get("mongo", "cases", self.case_id)
        self.assertEqual(mongo_case_after, self.mongo_case_before)

        mongo_order_after = _get("mongo", "orders", self.order_id)
        self.assertEqual(mongo_order_after, self.mongo_order_before)

        # negative bound: re-import under VIDUSHI_BACKEND=mongo (the rollback path) is
        # still meaningful because the source row is genuinely still there, unmutated.
        self.assertEqual(mongo_case_after["vendor"], "Migratron")
        self.assertEqual(mongo_order_after["merchant"], "Migratron")

    # ---- §S4 AC4: SQLite snapshot -> re-import round-trip is idempotent ----

    def test_sqlite_snapshot_then_reimport_round_trip_is_idempotent(self):
        counts_before = {t: _stats_total("sqlite", t) for t in store.STORES}
        case_before = _get("sqlite", "cases", self.case_id)
        order_before = _get("sqlite", "orders", self.order_id)

        _run("sqlite", "snapshot")
        _run("sqlite", "import")

        counts_after = {t: _stats_total("sqlite", t) for t in store.STORES}
        self.assertEqual(counts_after, counts_before)

        case_after = _get("sqlite", "cases", self.case_id)
        self.assertEqual(case_after, case_before)
        order_after = _get("sqlite", "orders", self.order_id)
        self.assertEqual(order_after, order_before)

        # negative bound: re-import did not duplicate rows for the seeded stores
        self.assertEqual(_stats_total("sqlite", "cases"), 1)
        self.assertEqual(_stats_total("sqlite", "orders"), 1)


if __name__ == "__main__":
    unittest.main()
