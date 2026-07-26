"""CR-OA-018 VERIFY-fix cycle — 4 acceptance-coverage gaps VERIFY found:

  §S1 an unknown `VIDUSHI_BACKEND` value must surface as a STRUCTURED CLI error (exit 1,
      a parseable `error` on stdout, no raw Python traceback on stderr) — like every other
      error verb in this CLI — instead of an uncaught `ValueError` traceback. Today
      `get_backend("bogus")` (`vidushi_oa/backends/__init__.py`) raises a bare `ValueError`
      that `vidushi_oa/_cli.py::main` does not catch, so `UnknownBackendCliErrorTest` is RED.

  §S2 a full SQLite lifecycle INCLUDING the three sweeps (`due-sweep`, `warranty-sweep`,
      `delivery-sweep`) — the same observable outcomes the pre-existing Mongo CLI test
      suite (`test_cr_oa_005_warranty_sweep.py`, `test_cr_oa_007_due_sweep.py`,
      `test_cr_oa_015_delivery_sweep.py`) asserts, driven end to end on `VIDUSHI_BACKEND=sqlite`
      instead. The sweep/attention/validate implementations were refactored to be
      backend-agnostic (CR-OA-018 §S3 GREEN) — `SqliteFullLifecycleSweepTest` is therefore
      a CHARACTERIZATION pass and MAY already be green; that is expected and reported as such.

  §S3 the packaged wheel installs and runs end to end in a CLEAN venv with NO pymongo (the
      SQLite-default path), and separately with the `[mongo]` extra (the opt-in Mongo path)
      — `CleanVenvPackagingTest`. Slow (builds the real wheel + two fresh venvs); skips
      cleanly if pip/network is unavailable.

  §S4 the Mongo -> SQLite migration validated against the REAL LIVE `vidushi_oa` Mongo
      database, READ-ONLY on that side (only `snapshot`/`stats`/`get` ever run against it;
      no `add`/`rm`/`update`/`init`) — `LiveMongoReadOnlyMigrationTest`. Skips cleanly if the
      live DB is empty or unreachable.

DATA SAFETY (§S2/§S3): every SQLite leg points `VIDUSHI_SQLITE_PATH` at a throwaway tempfile
and `VIDUSHI_DATA_DIR` at a throwaway tempdir — NEVER the real repo `data/` and NEVER the
real `~/.local/share/vidushi-oa/oa.db`. §S3's Mongo leg (if reached) uses a throwaway test
DB, never the real `vidushi_oa` DB. §S4 is the only class that touches the real `vidushi_oa`
Mongo DB, and only via `snapshot`/`stats`/`get`/direct pymongo reads.

`tests/conftest.py`'s autouse fixture pins `VIDUSHI_BACKEND=mongo` in the PARENT process
`os.environ` for every test. Every subprocess env dict built here is a COPY of
`os.environ` with `VIDUSHI_BACKEND` explicitly overridden (or popped, for the
default-backend leg of §S3) before the subprocess runs — the parent `os.environ` itself is
never mutated by this file.
"""
import datetime
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import pymongo

from vidushi_oa._cli import STORES

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, "scripts")
STORE = os.path.join(SCRIPTS, "store.py")
VENV_PYTHON = os.path.join(ROOT, ".venv", "bin", "python")


def _days_ago(n):
    return (datetime.date.today() - datetime.timedelta(days=n)).isoformat()


def _days_ahead(n):
    return (datetime.date.today() + datetime.timedelta(days=n)).isoformat()


# ── §S1 — unknown backend is a structured CLI error, not a traceback ─────────────────────

class UnknownBackendCliErrorTest(unittest.TestCase):
    """§S1 — `VIDUSHI_BACKEND=bogus` must behave like every other CLI error path: exit 1,
    a structured `error` on stdout, and NO raw traceback on stderr. No mongod/data dir
    needed — the failure happens before any store is touched."""

    def setUp(self):
        self.base_env = dict(os.environ)
        self.base_env["VIDUSHI_BACKEND"] = "bogus"

    def _run(self, *args, fmt="toon"):
        env = dict(self.base_env)
        env["VIDUSHI_FORMAT"] = fmt
        return subprocess.run(
            [sys.executable, STORE, *args], capture_output=True, text=True, env=env,
        )

    def test_unknown_backend_query_exits_1_with_structured_error_and_no_traceback(self):
        r = self._run("query", "cases")

        self.assertEqual(
            r.returncode, 1,
            f"unknown-backend run must exit 1, got {r.returncode}; "
            f"stdout={r.stdout!r} stderr={r.stderr!r}",
        )
        # positive: SOME structured error content reached stdout (not silently empty)
        self.assertNotEqual(r.stdout.strip(), "", "expected non-empty structured error on stdout")
        self.assertIn("error", r.stdout.lower(),
                       f"expected a structured 'error' on stdout, got: {r.stdout!r}")
        # negative bound: no raw Python traceback leaked to stderr
        self.assertNotIn("Traceback", r.stderr,
                          f"stderr must not contain a raw Python traceback, got: {r.stderr!r}")

    def test_unknown_backend_error_json_names_the_bad_backend(self):
        r = self._run("query", "cases", fmt="json")

        self.assertEqual(r.returncode, 1, f"stdout={r.stdout!r} stderr={r.stderr!r}")
        self.assertNotIn("Traceback", r.stderr, f"stderr={r.stderr!r}")
        payload = json.loads(r.stdout.strip())
        self.assertIsInstance(payload, dict, f"expected a JSON object error envelope, got {payload!r}")
        self.assertIn("error", payload)
        blob = json.dumps(payload).lower()
        self.assertIn("bogus", blob, f"expected the bad backend name 'bogus' surfaced in the "
                                      f"error payload, got: {payload!r}")

    def test_unknown_backend_get_also_exits_1_structured_not_500_traceback(self):
        """Same contract on a second verb (`get`), so the fix isn't `query`-specific."""
        r = self._run("get", "cases", "case_missing", fmt="json")

        self.assertEqual(r.returncode, 1, f"stdout={r.stdout!r} stderr={r.stderr!r}")
        self.assertNotIn("Traceback", r.stderr, f"stderr={r.stderr!r}")
        payload = json.loads(r.stdout.strip())
        self.assertIn("error", payload)


# ── §S2 — full SQLite lifecycle INCLUDING sweeps ──────────────────────────────────────────

class SqliteFullLifecycleSweepTest(unittest.TestCase):
    """§S2 — drives the REAL `voa` CLI, `VIDUSHI_BACKEND=sqlite`, through
    init -> add (subscriptions/warranties/orders) -> due-sweep -> warranty-sweep ->
    delivery-sweep -> attention -> validate, and checks the SAME observable outcomes the
    Mongo sweep test suites assert (`test_cr_oa_007_due_sweep.py`,
    `test_cr_oa_005_warranty_sweep.py`, `test_cr_oa_015_delivery_sweep.py`).

    Backend-agnostic sweep code means this MAY already be green — that is expected and
    reported honestly per test.
    """

    @classmethod
    def setUpClass(cls):
        cls.data_dir = tempfile.mkdtemp(prefix="oa-cr018-verify-sqlite-data-")
        cls.sqlite_dir = tempfile.mkdtemp(prefix="oa-cr018-verify-sqlite-db-")
        cls.sqlite_path = os.path.join(cls.sqlite_dir, "oa.db")

        cls.env = dict(os.environ)
        cls.env["VIDUSHI_BACKEND"] = "sqlite"
        cls.env["VIDUSHI_SQLITE_PATH"] = cls.sqlite_path
        cls.env["VIDUSHI_DATA_DIR"] = cls.data_dir
        cls.env["VIDUSHI_FORMAT"] = "json"

        cls._run("init")

        add_sub = cls._run("add", "subscriptions", "--json", json.dumps({
            "provider": "Sweepco", "status": "IN_PROGRESS", "disposition": "KEEP",
            "renews": _days_ahead(10), "plan": "premium",
        }))
        cls.sub_id = json.loads(add_sub.stdout.strip())["added"][0]

        add_war = cls._run("add", "warranties", "--json", json.dumps({
            "vendor": "Warrantco", "product": "Widget", "status": "IN_PROGRESS",
            "expiry": _days_ago(5), "acct": "personal",
        }))
        cls.war_id = json.loads(add_war.stdout.strip())["added"][0]

        add_ord = cls._run("add", "orders", "--json", json.dumps({
            "merchant": "StuckMerchant", "number": "O-STUCK-1", "amount": 10,
            "currency": "INR", "status": "IN_PROGRESS", "acct": "personal",
            "last_event_date": _days_ago(10),
        }))
        cls.order_id = json.loads(add_ord.stdout.strip())["added"][0]

        cls.due_result = json.loads(cls._run("due-sweep").stdout.strip())
        cls.warranty_result = json.loads(cls._run("warranty-sweep").stdout.strip())
        cls.delivery_result = json.loads(cls._run("delivery-sweep").stdout.strip())
        cls.attention_result = json.loads(cls._run("attention").stdout.strip())
        cls.validate_result = json.loads(cls._run("validate").stdout.strip())

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.data_dir, ignore_errors=True)
        shutil.rmtree(cls.sqlite_dir, ignore_errors=True)

    @classmethod
    def _run(cls, *args):
        result = subprocess.run(
            [sys.executable, STORE, *args], capture_output=True, text=True, env=cls.env,
        )
        assert result.returncode == 0, (
            f"store.py {' '.join(args)} failed (rc={result.returncode}): "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        return result

    @classmethod
    def _get(cls, type_, id_):
        result = cls._run("get", type_, id_)
        return json.loads(result.stdout.strip())

    # ---- due-sweep ----

    def test_due_sweep_reports_the_in_window_keep_subscription(self):
        self.assertIn(self.sub_id, self.due_result["due"]["subscriptions"],
                       f"expected {self.sub_id} in due-sweep output: {self.due_result}")
        self.assertFalse(self.due_result["dry_run"])

    def test_due_sweep_flips_status_due_and_opens_renewal_confirm_for_keep_disposition(self):
        doc = self._get("subscriptions", self.sub_id)
        self.assertEqual(doc["status"], "DUE")
        by_slug = {a["action"]: a for a in doc.get("actions", [])}
        self.assertIn("renewal-confirm", by_slug,
                       f"KEEP disposition must open 'renewal-confirm' (by_disposition), got: {doc.get('actions')}")
        self.assertEqual(by_slug["renewal-confirm"]["status"], "OPEN")
        # negative bound: the non-KEEP default action must NOT have been opened instead
        self.assertNotIn("cancel-before-charge", by_slug)

    # ---- warranty-sweep ----

    def test_warranty_sweep_reports_the_past_due_warranty(self):
        self.assertIn(self.war_id, self.warranty_result["expired"],
                       f"expected {self.war_id} in warranty-sweep output: {self.warranty_result}")
        self.assertFalse(self.warranty_result["dry_run"])

    def test_warranty_sweep_flips_status_expired_and_opens_renew_or_extend(self):
        doc = self._get("warranties", self.war_id)
        self.assertEqual(doc["status"], "EXPIRED")
        by_slug = {a["action"]: a for a in doc.get("actions", [])}
        self.assertIn("renew-or-extend", by_slug)
        self.assertEqual(by_slug["renew-or-extend"]["status"], "OPEN")

    # ---- delivery-sweep ----

    def test_delivery_sweep_reports_the_stale_order(self):
        self.assertIn(self.order_id, self.delivery_result["chased"],
                       f"expected {self.order_id} in delivery-sweep output: {self.delivery_result}")
        self.assertFalse(self.delivery_result["dry_run"])

    def test_delivery_sweep_opens_stuck_chase_without_flipping_status(self):
        doc = self._get("orders", self.order_id)
        # negative bound: delivery-sweep does NOT change status (unlike the other two sweeps)
        self.assertEqual(doc["status"], "IN_PROGRESS")
        by_slug = {a["action"]: a for a in doc.get("actions", [])}
        self.assertIn("stuck-chase", by_slug)
        self.assertEqual(by_slug["stuck-chase"]["status"], "OPEN")
        self.assertEqual(by_slug["stuck-chase"].get("owner"), "user")

    # ---- attention surfaces all three swept rows ----

    def test_attention_surfaces_exactly_the_three_swept_rows(self):
        by_id = {row["id"]: row for row in self.attention_result}
        self.assertIn(self.sub_id, by_id)
        self.assertIn(self.war_id, by_id)
        self.assertIn(self.order_id, by_id)
        # positive: correct type + open_actions per row
        self.assertEqual(by_id[self.sub_id]["type"], "subscriptions")
        self.assertIn("renewal-confirm", by_id[self.sub_id]["open_actions"])
        self.assertEqual(by_id[self.war_id]["type"], "warranties")
        self.assertIn("renew-or-extend", by_id[self.war_id]["open_actions"])
        self.assertEqual(by_id[self.order_id]["type"], "orders")
        self.assertIn("stuck-chase", by_id[self.order_id]["open_actions"])
        # negative bound: exactly these 3 rows on a freshly-provisioned db, nothing extra
        self.assertEqual(len(self.attention_result), 3,
                          f"expected exactly 3 rows needing attention, got: {self.attention_result}")

    # ---- validate is clean across every store ----

    def test_validate_is_clean_for_every_store(self):
        self.assertEqual(set(self.validate_result.keys()), set(STORES),
                          f"expected a validate entry for every store type, got: {list(self.validate_result)}")
        for t, ids in self.validate_result.items():
            self.assertEqual(ids, [], f"{t}: sqlite validate found nonconforming ids: {ids}")


# ── §S3 — clean-venv (no pymongo) end-to-end + [mongo] extra ─────────────────────────────

MONGO_EXTRA_TEST_DB = "vidushi_oa_test_cr018_s3"


class CleanVenvPackagingTest(unittest.TestCase):
    """§S3 — builds the real wheel once, then proves it installs+runs in TWO fresh, isolated
    venvs: (a) plain install (no pymongo) on the SQLite-default path, (b) the `[mongo]`
    extra installing pymongo and reaching the (throwaway test) Mongo DB. Slow; skips
    cleanly on pip/network unavailability, matching the precedent in
    `test_cr_oa_018_docs_uv.py::UvToolInstallEndToEndTest`.
    """

    @classmethod
    def setUpClass(cls):
        cls.build_dir = tempfile.mkdtemp(prefix="oa-cr018-verify-wheel-")
        result = subprocess.run(
            [VENV_PYTHON, "-m", "build", "--wheel", "--outdir", cls.build_dir],
            cwd=ROOT, capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            raise unittest.SkipTest(
                f"wheel build failed, cannot run §S3 packaging tests: "
                f"stdout={result.stdout}\nstderr={result.stderr}"
            )
        wheels = glob.glob(os.path.join(cls.build_dir, "*.whl"))
        if len(wheels) != 1:
            raise unittest.SkipTest(f"expected exactly one built wheel, found: {wheels}")
        cls.wheel_path = wheels[0]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.build_dir, ignore_errors=True)

    def _make_venv(self, prefix):
        tmp = tempfile.mkdtemp(prefix=prefix)
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        result = subprocess.run(
            [sys.executable, "-m", "venv", tmp], capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            self.skipTest(f"could not create throwaway venv: {result.stderr}")
        return tmp

    def _pip_install(self, venv_dir, *pip_args):
        pip = os.path.join(venv_dir, "bin", "pip")
        try:
            result = subprocess.run(
                [pip, "install", *pip_args], capture_output=True, text=True, timeout=300,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            self.skipTest(f"pip install unavailable: {exc}")
        if result.returncode != 0:
            self.skipTest(f"pip install {pip_args} failed (likely no network): {result.stderr}")

    # ---- (a) plain install: no pymongo, sqlite-default end-to-end ----

    def test_plain_install_has_no_pymongo_and_sqlite_round_trips_end_to_end(self):
        v1 = self._make_venv("oa-cr018-verify-v1-")
        self._pip_install(v1, self.wheel_path)

        voa = os.path.join(v1, "bin", "voa")
        self.assertTrue(os.path.isfile(voa), f"expected an installed 'voa' console script at {voa}")

        # positive: NO pymongo landed in this venv (plain install, no [mongo] extra)
        pymongo_check = subprocess.run(
            [os.path.join(v1, "bin", "python"), "-c", "import pymongo"],
            capture_output=True, text=True, timeout=30,
        )
        self.assertNotEqual(
            pymongo_check.returncode, 0,
            "expected 'import pymongo' to FAIL in a plain (no [mongo] extra) install, "
            f"but it succeeded: stdout={pymongo_check.stdout!r}",
        )

        data_dir = tempfile.mkdtemp(prefix="oa-cr018-verify-v1-data-")
        self.addCleanup(shutil.rmtree, data_dir, ignore_errors=True)
        sqlite_path = os.path.join(data_dir, "oa.db")

        env = dict(os.environ)
        env.pop("VIDUSHI_BACKEND", None)  # NO backend set -> must default to sqlite (§S3)
        env["VIDUSHI_SQLITE_PATH"] = sqlite_path
        env["VIDUSHI_DATA_DIR"] = data_dir
        env["VIDUSHI_FORMAT"] = "json"

        setup_result = subprocess.run(
            [voa, "setup"], capture_output=True, text=True, env=env, timeout=60,
        )
        self.assertEqual(setup_result.returncode, 0,
                          f"'voa setup' failed with no VIDUSHI_BACKEND set (default sqlite): "
                          f"stdout={setup_result.stdout!r} stderr={setup_result.stderr!r}")

        add_result = subprocess.run(
            [voa, "add", "subscriptions", "--json",
             json.dumps({"provider": "CleanVenvCo", "status": "NEW"})],
            capture_output=True, text=True, env=env, timeout=60,
        )
        self.assertEqual(add_result.returncode, 0, f"stderr={add_result.stderr!r}")
        added = json.loads(add_result.stdout.strip())["added"]
        self.assertEqual(len(added), 1)
        sub_id = added[0]

        query_result = subprocess.run(
            [voa, "query", "subscriptions"], capture_output=True, text=True, env=env, timeout=60,
        )
        self.assertEqual(query_result.returncode, 0, f"stderr={query_result.stderr!r}")
        rows = json.loads(query_result.stdout.strip())
        ids = [r["id"] for r in rows]
        self.assertIn(sub_id, ids, f"expected the added row back from query, got ids: {ids}")
        by_id = {r["id"]: r for r in rows}
        self.assertEqual(by_id[sub_id]["provider"], "CleanVenvCo")

    # ---- (b) [mongo] extra: pymongo present, real Mongo round-trip on a test DB ----

    def test_mongo_extra_install_has_pymongo_and_queries_test_mongo(self):
        v2 = self._make_venv("oa-cr018-verify-v2-")
        self._pip_install(v2, f"{self.wheel_path}[mongo]")

        voa = os.path.join(v2, "bin", "voa")
        self.assertTrue(os.path.isfile(voa))

        # positive: pymongo IS present with the [mongo] extra
        pymongo_check = subprocess.run(
            [os.path.join(v2, "bin", "python"), "-c", "import pymongo"],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(
            pymongo_check.returncode, 0,
            f"expected 'import pymongo' to succeed with the [mongo] extra installed: "
            f"stderr={pymongo_check.stderr!r}",
        )

        client = pymongo.MongoClient("mongodb://127.0.0.1:27017", serverSelectionTimeoutMS=2000)
        try:
            client.admin.command("ping")
        except Exception as exc:
            client.close()
            self.skipTest(f"local mongod not reachable: {exc}")

        data_dir = tempfile.mkdtemp(prefix="oa-cr018-verify-v2-data-")
        self.addCleanup(shutil.rmtree, data_dir, ignore_errors=True)
        env = dict(os.environ)
        env["VIDUSHI_BACKEND"] = "mongo"
        env["VIDUSHI_MONGO_DB"] = MONGO_EXTRA_TEST_DB
        env["VIDUSHI_DATA_DIR"] = data_dir
        env["VIDUSHI_FORMAT"] = "json"

        try:
            add_result = subprocess.run(
                [voa, "add", "cases", "--json",
                 json.dumps({"vendor": "MongoExtraCo", "status": "NEW"})],
                capture_output=True, text=True, env=env, timeout=60,
            )
            self.assertEqual(add_result.returncode, 0, f"stderr={add_result.stderr!r}")
            added = json.loads(add_result.stdout.strip())["added"]
            self.assertEqual(len(added), 1)
            case_id = added[0]

            query_result = subprocess.run(
                [voa, "query", "cases", "--json"],
                capture_output=True, text=True, env=env, timeout=60,
            )
            self.assertEqual(query_result.returncode, 0, f"stderr={query_result.stderr!r}")
            rows = json.loads(query_result.stdout.strip())
            ids = [r["id"] for r in rows]
            self.assertIn(case_id, ids, f"expected the added row back from query, got ids: {ids}")

            # cross-check straight against Mongo (independent of the CLI's own read path)
            raw = client[MONGO_EXTRA_TEST_DB]["cases"].find_one({"id": case_id})
            self.assertIsNotNone(raw)
            self.assertEqual(raw["vendor"], "MongoExtraCo")
        finally:
            client.drop_database(MONGO_EXTRA_TEST_DB)
            client.close()


# ── §S4 — migration validated against the LIVE data, READ-ONLY ───────────────────────────

class LiveMongoReadOnlyMigrationTest(unittest.TestCase):
    """§S4 — snapshots the REAL live `vidushi_oa` Mongo DB (default `VIDUSHI_MONGO_DB`,
    default `VIDUSHI_MONGO_URI`) to a throwaway jsonl dir, imports it into a throwaway
    SQLite db, and checks per-store totals + `validate` cleanliness match, WITHOUT ever
    issuing a write verb (`add`/`rm`/`update`/`init`/`apply-validators`) against the live
    Mongo side. Only `snapshot`/`stats`/`get` run against it, plus direct read-only pymongo
    counts for an independent cross-check that the live DB is untouched afterward.

    Skips cleanly if the live DB is empty or unreachable — this is a characterization test
    over REAL data, not a fixture the test controls.
    """

    @classmethod
    def setUpClass(cls):
        try:
            cls.client = pymongo.MongoClient(
                "mongodb://127.0.0.1:27017", serverSelectionTimeoutMS=2000)
            cls.client.admin.command("ping")
        except Exception as exc:
            raise unittest.SkipTest(f"local mongod not reachable: {exc}")

        cls.live_db = cls.client["vidushi_oa"]  # the REAL default DB name — read-only below
        cls.live_totals_before = {t: cls.live_db[t].count_documents({}) for t in STORES}
        if sum(cls.live_totals_before.values()) == 0:
            cls.client.close()
            raise unittest.SkipTest("live 'vidushi_oa' Mongo DB is empty; nothing to migrate")

        cls.data_dir = tempfile.mkdtemp(prefix="oa-cr018-verify-livemig-data-")
        cls.sqlite_dir = tempfile.mkdtemp(prefix="oa-cr018-verify-livemig-sqlite-")
        cls.sqlite_path = os.path.join(cls.sqlite_dir, "oa.db")

        # ---- READ-ONLY leg against the REAL live Mongo DB: snapshot only ----
        mongo_env = dict(os.environ)
        mongo_env["VIDUSHI_BACKEND"] = "mongo"
        mongo_env.pop("VIDUSHI_MONGO_DB", None)  # unset -> resolves to the REAL default 'vidushi_oa'
        mongo_env["VIDUSHI_DATA_DIR"] = cls.data_dir  # snapshot writes JSONL here, never repo data/
        mongo_env["VIDUSHI_FORMAT"] = "json"
        cls.mongo_env = mongo_env

        snap = subprocess.run(
            [sys.executable, STORE, "snapshot"], capture_output=True, text=True, env=mongo_env,
        )
        if snap.returncode != 0:
            cls.client.close()
            raise unittest.SkipTest(
                f"'voa snapshot' against the live Mongo DB failed: "
                f"stdout={snap.stdout!r} stderr={snap.stderr!r}"
            )
        cls.snapshot_result = json.loads(snap.stdout.strip())

        # ---- write leg is entirely on the throwaway SQLite side ----
        sqlite_env = dict(os.environ)
        sqlite_env["VIDUSHI_BACKEND"] = "sqlite"
        sqlite_env["VIDUSHI_SQLITE_PATH"] = cls.sqlite_path
        sqlite_env["VIDUSHI_DATA_DIR"] = cls.data_dir
        sqlite_env["VIDUSHI_FORMAT"] = "json"
        cls.sqlite_env = sqlite_env

        init = subprocess.run(
            [sys.executable, STORE, "init"], capture_output=True, text=True, env=sqlite_env,
        )
        assert init.returncode == 0, f"sqlite init failed: {init.stdout!r} {init.stderr!r}"

        imp = subprocess.run(
            [sys.executable, STORE, "import"], capture_output=True, text=True, env=sqlite_env,
        )
        assert imp.returncode == 0, (
            f"sqlite import of the live-data snapshot failed: "
            f"stdout={imp.stdout!r} stderr={imp.stderr!r}"
        )
        cls.import_result = json.loads(imp.stdout.strip())

        cls.validate_result = json.loads(
            subprocess.run([sys.executable, STORE, "validate"],
                            capture_output=True, text=True, env=sqlite_env).stdout.strip()
        )

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.data_dir, ignore_errors=True)
        shutil.rmtree(cls.sqlite_dir, ignore_errors=True)
        cls.client.close()

    def _sqlite_total(self, type_):
        r = subprocess.run(
            [sys.executable, STORE, "stats", type_], capture_output=True, text=True, env=self.sqlite_env,
        )
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr!r}")
        return json.loads(r.stdout.strip())["total"]

    def test_snapshot_counts_match_the_live_mongo_totals_per_store(self):
        for t in STORES:
            self.assertEqual(
                self.snapshot_result["snapshot"][t], self.live_totals_before[t],
                f"{t}: snapshot count ({self.snapshot_result['snapshot'][t]}) != "
                f"live mongo total ({self.live_totals_before[t]})",
            )

    def test_sqlite_totals_after_import_match_live_mongo_totals_per_store(self):
        for t in STORES:
            sqlite_total = self._sqlite_total(t)
            self.assertEqual(
                sqlite_total, self.live_totals_before[t],
                f"{t}: sqlite total after import ({sqlite_total}) != "
                f"live mongo total ({self.live_totals_before[t]})",
            )

    def test_sqlite_validate_is_clean_after_migrating_live_data(self):
        self.assertEqual(set(self.validate_result.keys()), set(STORES))
        for t, ids in self.validate_result.items():
            self.assertEqual(
                ids, [],
                f"{t}: sqlite validate found nonconforming ids after migrating LIVE data: {ids}",
            )

    def test_live_mongo_totals_unchanged_after_the_read_only_migration(self):
        # independent re-read straight through pymongo (bypassing the CLI entirely)
        for t in STORES:
            after = self.live_db[t].count_documents({})
            self.assertEqual(
                after, self.live_totals_before[t],
                f"{t}: LIVE mongo total changed during a supposedly read-only migration "
                f"({self.live_totals_before[t]} -> {after})",
            )

    def test_live_mongo_totals_unchanged_via_voa_stats_too(self):
        # same check, but through the CLI's own 'stats' verb (still read-only)
        for t in STORES:
            r = subprocess.run(
                [sys.executable, STORE, "stats", t],
                capture_output=True, text=True, env=self.mongo_env,
            )
            self.assertEqual(r.returncode, 0, f"stderr={r.stderr!r}")
            after = json.loads(r.stdout.strip())["total"]
            self.assertEqual(after, self.live_totals_before[t])


if __name__ == "__main__":
    unittest.main()
