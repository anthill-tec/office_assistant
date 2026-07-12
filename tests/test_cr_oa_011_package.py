"""CR-OA-011 Cycle B — §S1/§S2 package restructure + `pyproject.toml` + `voa` console.

Verifies the NEW-contract ACs before the restructure lands:

  §S1 `scripts/{store,oa_mongo,transitions,oa_toon}.py` -> a `vidushi_oa/` package
      (`cli.py`, `mongo.py`, `transitions.py`, `toon.py`, `__init__.py`); `data/schema/*.json`
      -> `vidushi_oa/schema/*.json` loaded via `importlib.resources`; `scripts/store.py` (and
      the other `scripts/*.py` entry points) stay thin compat shims re-exporting from
      `vidushi_oa` so the existing `import oa_mongo` / `import store` tests keep working.
  §S2 a `pyproject.toml` (hatchling) at the repo root: dist name `vidushi-oa`, console
      script `voa = vidushi_oa.cli:main`, `pymongo` + `python-toon` dependencies, and the
      schema JSON declared as package data so it ships inside the wheel.

Today NEITHER `vidushi_oa/` NOR `pyproject.toml` exist — `scripts/{store,oa_mongo,
transitions,oa_toon}.py` are the only implementation, and `data/schema/*.json` is the only
schema location. Every test below MUST fail (mostly as `ModuleNotFoundError` / `FileNotFoundError`
/ `AssertionError`) until CR-OA-011 Cycle B's GREEN phase lands the package + pyproject.

DATA SAFETY: the one end-to-end test (#5) isolates via `VIDUSHI_MONGO_DB=vidushi_oa_test`
(dropped in tearDown). Requires a local mongod on 127.0.0.1:27017 (the office_assistant
instance; CR-OA-001). All other tests touch no Mongo.
"""
import importlib
import json
import os
import subprocess
import sys
import tomllib
import unittest

import pymongo

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, "scripts")
STORE = os.path.join(SCRIPTS, "store.py")
PYPROJECT = os.path.join(ROOT, "pyproject.toml")

TEST_DB = "vidushi_oa_test"

STORE_TYPES = ["contacts", "invoices", "warranties", "cases", "products",
               "subscriptions", "insurance"]


def _ensure_root_on_path():
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)


def _ensure_scripts_on_path():
    if SCRIPTS not in sys.path:
        sys.path.insert(0, SCRIPTS)


def _fresh_import(name):
    """Import `name`, forcing a fresh module object even if a stale one is cached
    (e.g. from an earlier test in the same process) so we always see the module as it
    exists on disk right now."""
    if name in sys.modules:
        del sys.modules[name]
    return importlib.import_module(name)


class PackageImportsTest(unittest.TestCase):
    """§S1 — the top-level `vidushi_oa` package exposes the expected modules/symbols."""

    def setUp(self):
        _ensure_root_on_path()

    def test_cli_module_exposes_callable_main(self):
        cli = _fresh_import("vidushi_oa.cli")
        self.assertTrue(hasattr(cli, "main"), "vidushi_oa.cli must expose `main`")
        self.assertTrue(callable(cli.main), "vidushi_oa.cli.main must be callable")

    def test_mongo_module_exposes_client_db_coll(self):
        mongo = _fresh_import("vidushi_oa.mongo")
        for name in ("client", "db", "coll"):
            self.assertTrue(hasattr(mongo, name), f"vidushi_oa.mongo must expose `{name}`")
            self.assertTrue(callable(getattr(mongo, name)), f"vidushi_oa.mongo.{name} must be callable")

    def test_toon_module_round_trips_to_toon_from_toon(self):
        toon = _fresh_import("vidushi_oa.toon")
        self.assertTrue(hasattr(toon, "to_toon"))
        self.assertTrue(hasattr(toon, "from_toon"))
        payload = [{"id": "x1", "name": "widget"}]
        encoded = toon.to_toon(payload)
        # positive: encoding actually produced TOON text, not a re-serialized JSON array
        self.assertIsInstance(encoded, str)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(encoded)
        # round-trip: decoding the encoded TOON recovers the original data
        decoded = toon.from_toon(encoded)
        self.assertEqual(decoded, payload)

    def test_transitions_module_exposes_transitions_and_find_transition(self):
        transitions = _fresh_import("vidushi_oa.transitions")
        self.assertTrue(hasattr(transitions, "TRANSITIONS"))
        self.assertTrue(hasattr(transitions, "find_transition"))
        self.assertIsInstance(transitions.TRANSITIONS, dict)
        # positive bound: at least the warranties domain has transitions defined
        self.assertIn("warranties", transitions.TRANSITIONS)
        self.assertGreater(len(transitions.TRANSITIONS["warranties"]), 0)
        # negative: an unknown (status, event) pair yields no transition
        self.assertIsNone(
            transitions.find_transition("warranties", "NEW", "not-a-real-event")
        )


class SchemaPackageDataTest(unittest.TestCase):
    """§S1 — `data/schema/*.json` -> `vidushi_oa/schema/*.json`, loaded as package data."""

    def setUp(self):
        _ensure_root_on_path()

    def test_a_schema_file_is_package_data_and_parses_with_properties(self):
        import importlib.resources as res
        resource = res.files("vidushi_oa").joinpath("schema/invoices.schema.json")
        self.assertTrue(resource.is_file(), "vidushi_oa/schema/invoices.schema.json must exist as package data")
        parsed = json.loads(resource.read_text(encoding="utf-8"))
        self.assertIsInstance(parsed, dict)
        self.assertIn("properties", parsed)
        self.assertIsInstance(parsed["properties"], dict)
        # positive: real invoice fields present (proves it's the actual schema, not a stub)
        self.assertIn("id", parsed["properties"])

    def test_all_seven_store_schemas_present_under_package(self):
        import importlib.resources as res
        schema_dir = res.files("vidushi_oa").joinpath("schema")
        present = set()
        for t in STORE_TYPES:
            entry = schema_dir.joinpath(f"{t}.schema.json")
            if entry.is_file():
                present.add(t)
        self.assertEqual(
            present, set(STORE_TYPES),
            f"expected all 7 store schemas under vidushi_oa/schema/, found: {sorted(present)}",
        )
        # negative bound: exactly 7, not more (no stray/duplicate schema files)
        all_json = [p.name for p in schema_dir.iterdir() if p.name.endswith(".schema.json")]
        self.assertEqual(len(all_json), 7, f"expected exactly 7 schema files, found: {sorted(all_json)}")


class CompatShimTest(unittest.TestCase):
    """§S1 — the old `scripts/{oa_mongo,store,oa_toon}.py` import paths still resolve,
    by re-exporting from the new `vidushi_oa` package (not by re-implementing)."""

    def setUp(self):
        _ensure_root_on_path()
        _ensure_scripts_on_path()

    def test_scripts_oa_mongo_shim_reexports_vidushi_oa_mongo_db(self):
        oa_mongo = _fresh_import("oa_mongo")
        vmongo = _fresh_import("vidushi_oa.mongo")
        self.assertTrue(hasattr(oa_mongo, "db"), "scripts/oa_mongo.py shim must still expose `db`")
        # identity, not just duck-typed equivalence: the shim re-exports the real symbol
        self.assertIs(
            oa_mongo.db, vmongo.db,
            "scripts/oa_mongo.db must be the SAME object as vidushi_oa.mongo.db (re-export, not reimplementation)",
        )

    def test_scripts_store_shim_reexports_vidushi_oa_cli_main(self):
        store = _fresh_import("store")
        vcli = _fresh_import("vidushi_oa.cli")
        self.assertTrue(hasattr(store, "main"), "scripts/store.py shim must still expose `main`")
        self.assertIs(
            store.main, vcli.main,
            "scripts/store.main must be the SAME object as vidushi_oa.cli.main (re-export, not reimplementation)",
        )

    def test_scripts_oa_toon_shim_reexports_vidushi_oa_toon_to_toon(self):
        oa_toon = _fresh_import("oa_toon")
        vtoon = _fresh_import("vidushi_oa.toon")
        self.assertTrue(hasattr(oa_toon, "to_toon"), "scripts/oa_toon.py shim must still expose `to_toon`")
        self.assertIs(
            oa_toon.to_toon, vtoon.to_toon,
            "scripts/oa_toon.to_toon must be the SAME object as vidushi_oa.toon.to_toon (re-export, not reimplementation)",
        )


class PyprojectTest(unittest.TestCase):
    """§S2 — `pyproject.toml` declares the `vidushi-oa` distribution, the `voa` console
    script, its runtime dependencies, and the schema JSON as package data."""

    def setUp(self):
        self.assertTrue(os.path.exists(PYPROJECT), f"expected {PYPROJECT} to exist")
        with open(PYPROJECT, "rb") as f:
            self.data = tomllib.load(f)
        with open(PYPROJECT, encoding="utf-8") as f:
            self.text = f.read()

    def test_project_name_is_vidushi_oa(self):
        self.assertIn("project", self.data)
        self.assertEqual(self.data["project"].get("name"), "vidushi-oa")

    def test_console_script_voa_maps_to_vidushi_oa_cli_main(self):
        scripts = self.data["project"].get("scripts", {})
        self.assertEqual(
            scripts.get("voa"), "vidushi_oa.cli:main",
            f"expected [project.scripts] voa = 'vidushi_oa.cli:main', got: {scripts}",
        )
        # negative: the old `oa` console name is not declared
        self.assertNotIn("oa", scripts)

    def test_dependencies_include_pymongo_and_python_toon(self):
        deps = self.data["project"].get("dependencies", [])
        self.assertTrue(deps, "expected [project].dependencies to be non-empty")
        joined = " ".join(deps).lower()
        self.assertTrue(
            any(d.lower().startswith("pymongo") for d in deps),
            f"expected a pymongo dependency, got: {deps}",
        )
        self.assertTrue(
            any(d.lower().replace("_", "-").startswith("python-toon") for d in deps),
            f"expected a python-toon dependency, got: {deps}",
        )
        self.assertIn("pymongo", joined)

    def test_schema_json_declared_as_package_data(self):
        # the exact hatchling knob varies (force-include / artifacts / package-data-ish
        # config), so assert the pyproject TEXT references the schema directory under the
        # wheel/package-data configuration rather than pinning one specific TOML shape.
        self.assertIn("schema", self.text.lower())
        self.assertIn("vidushi_oa", self.text)


class ShimEndToEndTest(unittest.TestCase):
    """§S1/§S2 regression seam — the compat shim still runs end-to-end as a subprocess,
    proving `scripts/store.py` delegates to the real `vidushi_oa` package rather than
    just satisfying import-time symbol checks."""

    def setUp(self):
        self.client = pymongo.MongoClient("mongodb://127.0.0.1:27017", serverSelectionTimeoutMS=2000)
        self.client.drop_database(TEST_DB)
        self.client[TEST_DB]["invoices"].insert_one(
            {"id": "doc_cr011b_shim", "vendor": "Acme", "status": "NEW"}
        )

    def tearDown(self):
        self.client.drop_database(TEST_DB)
        self.client.close()

    def test_store_shim_stats_end_to_end_via_subprocess(self):
        # First prove the shim module object actually DELEGATES to vidushi_oa — otherwise
        # this test would trivially "pass" today just because scripts/store.py hasn't been
        # restructured yet, without proving anything about the new package.
        _ensure_root_on_path()
        _ensure_scripts_on_path()
        store = _fresh_import("store")
        vcli = _fresh_import("vidushi_oa.cli")
        self.assertIs(
            store.main, vcli.main,
            "scripts/store.py must delegate to vidushi_oa.cli.main before the subprocess call below means anything",
        )

        env = dict(os.environ)
        env["VIDUSHI_MONGO_DB"] = TEST_DB

        result = subprocess.run(
            [sys.executable, STORE, "stats", "invoices", "--json"],
            capture_output=True, text=True, env=env,
        )

        self.assertEqual(result.returncode, 0, f"shim invocation failed: {result.stderr}")
        parsed = json.loads(result.stdout)
        self.assertIn("total", parsed)
        self.assertEqual(parsed["total"], 1)
        # negative bound: exactly the one seeded row, not some stray accumulation
        self.assertNotEqual(parsed["total"], 0)


if __name__ == "__main__":
    unittest.main()
