"""CR-OA-011 Cycle C — §S5 `setup` verb (Mongo provisioning/verify).

Verifies the NEW-contract ACs before the `setup` verb lands in `vidushi_oa/_cli.py`
(reachable via both the `scripts/store.py` shim and, per AC regression, `voa`):

  - `setup` is a real argparse subparser (`store.py setup --help` -> rc 0, not
    argparse's "invalid choice" error).
  - `setup --check` ONLY diagnoses the `VIDUSHI_MONGO_URI` connection: rc 0 +
    an "OK" indication when Mongo is reachable.
  - `setup --check` against an unreachable URI (nothing listening) -> rc != 0
    within a few seconds (a SHORT serverSelectionTimeoutMS, not the pymongo
    default ~30s) + actionable guidance on stdout/stderr.
  - `setup` (no `--check`) on success runs the equivalent of `init`: creates
    each store's collection with a `$jsonSchema` validator and a unique `id`
    index, and `validate` returns `[]` afterwards.

Today NO `setup` verb exists at all — `store.py setup --help` fails with
argparse's "invalid choice: 'setup'" (rc 2). Every test in this module MUST
fail until CR-OA-011 Cycle C's GREEN phase adds the verb.

DATA SAFETY: isolates via `VIDUSHI_MONGO_DB=vidushi_oa_test` (dropped in
setUp/tearDown) and a throwaway `tempfile.mkdtemp()` for `VIDUSHI_DATA_DIR`
(removed in tearDown). Requires a local mongod on 127.0.0.1:27017 (the
office_assistant instance; CR-OA-001).
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

import pymongo

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, "scripts")
STORE = os.path.join(SCRIPTS, "store.py")

sys.path.insert(0, SCRIPTS)

TEST_DB = "vidushi_oa_test"
STORE_TYPES = ["contacts", "invoices", "warranties", "cases", "products",
               "subscriptions", "insurance"]
# Nothing should be listening here — used to prove the unreachable-connection path.
UNREACHABLE_URI = "mongodb://127.0.0.1:59999"


class SetupVerbExistsTest(unittest.TestCase):
    """§S5 AC1 — `setup` is a real subparser, not an unrecognized verb."""

    def test_setup_help_is_a_recognized_subcommand(self):
        result = subprocess.run(
            [sys.executable, STORE, "setup", "--help"],
            capture_output=True, text=True,
        )
        # positive: argparse's own help path (rc 0), proving `setup` parses as a subcommand
        self.assertEqual(
            result.returncode, 0,
            f"expected `store.py setup --help` to succeed, got rc={result.returncode} "
            f"stderr={result.stderr!r}",
        )
        self.assertIn("setup", result.stdout)
        # negative bound: NOT argparse's "invalid choice" rejection (today's actual RED state)
        self.assertNotIn("invalid choice", result.stderr.lower())


class SetupCheckReachableTest(unittest.TestCase):
    """§S5 AC2 — `setup --check` against the default (reachable) local Mongo -> rc 0
    + an explicit OK/reachable indication."""

    def setUp(self):
        self.client = pymongo.MongoClient("mongodb://127.0.0.1:27017", serverSelectionTimeoutMS=2000)
        self.tmpdir = tempfile.mkdtemp(prefix="oa-cr011c-setup-")

    def tearDown(self):
        self.client.drop_database(TEST_DB)
        self.client.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _env(self, uri=None):
        env = dict(os.environ)
        env["VIDUSHI_MONGO_DB"] = TEST_DB
        env["VIDUSHI_DATA_DIR"] = self.tmpdir
        if uri is not None:
            env["VIDUSHI_MONGO_URI"] = uri
        else:
            env.pop("VIDUSHI_MONGO_URI", None)
        return env

    def test_check_reachable_mongo_returns_zero_with_ok_message(self):
        result = subprocess.run(
            [sys.executable, STORE, "setup", "--check"],
            capture_output=True, text=True, env=self._env(),
        )
        self.assertEqual(
            result.returncode, 0,
            f"expected rc 0 for a reachable Mongo, got rc={result.returncode} "
            f"stdout={result.stdout!r} stderr={result.stderr!r}",
        )
        combined = (result.stdout + result.stderr).lower()
        # positive: an explicit OK/reachable indication (or the DB name), not silence
        self.assertTrue(
            any(kw in combined for kw in ("ok", "reachable", TEST_DB.lower())),
            f"expected an OK/reachable indication, got stdout={result.stdout!r} stderr={result.stderr!r}",
        )
        # negative bound: no traceback / unhandled exception leaked to the user
        self.assertNotIn("traceback", combined)


class SetupCheckUnreachableTest(unittest.TestCase):
    """§S5 AC2/AC5 — `setup --check` against an unreachable URI -> non-zero + guidance,
    and returns FAST (short serverSelectionTimeoutMS), not pymongo's ~30s default."""

    def _env(self):
        env = dict(os.environ)
        env["VIDUSHI_MONGO_URI"] = UNREACHABLE_URI
        env["VIDUSHI_MONGO_DB"] = TEST_DB
        env["VIDUSHI_DATA_DIR"] = tempfile.mkdtemp(prefix="oa-cr011c-unreach-")
        return env

    def test_check_unreachable_mongo_returns_nonzero_with_guidance_fast(self):
        env = self._env()
        start = time.monotonic()
        result = subprocess.run(
            [sys.executable, STORE, "setup", "--check"],
            capture_output=True, text=True, env=env, timeout=15,
        )
        elapsed = time.monotonic() - start
        shutil.rmtree(env["VIDUSHI_DATA_DIR"], ignore_errors=True)

        # positive: non-zero exit for an unreachable connection
        self.assertNotEqual(
            result.returncode, 0,
            f"expected a non-zero rc for an unreachable Mongo, got rc=0 stdout={result.stdout!r}",
        )
        # bound: fast failure -> proves a SHORT serverSelectionTimeoutMS was used, not
        # pymongo's ~30s default (which would blow well past this bound)
        self.assertLess(
            elapsed, 10.0,
            f"setup --check took {elapsed:.1f}s against an unreachable URI — "
            f"expected a short serverSelectionTimeoutMS so it fails fast",
        )
        combined = (result.stdout + result.stderr).lower()
        # positive: actionable guidance mentioning mongo/connect/the default port
        self.assertTrue(
            any(kw in combined for kw in ("mongo", "connect", "27017")),
            f"expected actionable guidance, got stdout={result.stdout!r} stderr={result.stderr!r}",
        )


class SetupProvisionsFreshDbTest(unittest.TestCase):
    """§S5 AC4 — `setup` (no `--check`) verifies the connection then runs `init`:
    creates each store's collection with a `$jsonSchema` validator + a unique `id`
    index, equivalent to running `init` directly."""

    def setUp(self):
        self.client = pymongo.MongoClient("mongodb://127.0.0.1:27017", serverSelectionTimeoutMS=2000)
        self.client.drop_database(TEST_DB)
        self.tmpdir = tempfile.mkdtemp(prefix="oa-cr011c-provision-")

    def tearDown(self):
        self.client.drop_database(TEST_DB)
        self.client.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _env(self):
        env = dict(os.environ)
        env["VIDUSHI_MONGO_DB"] = TEST_DB
        env["VIDUSHI_DATA_DIR"] = self.tmpdir
        return env

    def test_setup_on_fresh_db_creates_collections_indexes_and_validators(self):
        result = subprocess.run(
            [sys.executable, STORE, "setup"],
            capture_output=True, text=True, env=self._env(),
        )
        self.assertEqual(
            result.returncode, 0,
            f"expected `setup` to succeed against a fresh, reachable DB, got rc={result.returncode} "
            f"stdout={result.stdout!r} stderr={result.stderr!r}",
        )

        db = self.client[TEST_DB]
        collections = set(db.list_collection_names())
        # positive: all 7 store collections exist
        self.assertEqual(
            set(STORE_TYPES) & collections, set(STORE_TYPES),
            f"expected all 7 store collections, found: {sorted(collections)}",
        )

        for t in STORE_TYPES:
            info = db.command("listCollections", filter={"name": t})
            opts = info["cursor"]["firstBatch"][0]["options"]
            # positive: a $jsonSchema validator is attached
            self.assertIn("validator", opts, f"{t}: expected a validator to be attached by setup")
            self.assertIn("$jsonSchema", opts["validator"], f"{t}: expected a $jsonSchema validator")

            indexes = db[t].index_information()
            id_indexes = [
                spec for name, spec in indexes.items()
                if spec.get("key") == [("id", 1)]
            ]
            self.assertTrue(id_indexes, f"{t}: expected a unique index on `id`, indexes={indexes}")
            self.assertTrue(
                id_indexes[0].get("unique"), f"{t}: expected the `id` index to be unique, got {id_indexes[0]}"
            )

        # negative bound: exactly the 7 store collections created by setup, no stray extras
        # sneaking in via a broken implementation (system collections aside)
        stray = collections - set(STORE_TYPES) - {"system.views"}
        self.assertEqual(stray, set(), f"unexpected extra collections after setup: {stray}")

    def test_setup_result_is_validate_clean_for_a_couple_of_types(self):
        setup_result = subprocess.run(
            [sys.executable, STORE, "setup"],
            capture_output=True, text=True, env=self._env(),
        )
        # gate: `setup` itself must have succeeded (a stub/missing verb must fail HERE,
        # not slip through to a vacuous validate == [] on a collection setup never touched)
        self.assertEqual(
            setup_result.returncode, 0,
            f"setup failed: rc={setup_result.returncode} stderr={setup_result.stderr!r}",
        )
        # gate: `setup` actually created the collections (proves the subsequent
        # validate == [] reflects real provisioning, not an absent/empty collection)
        db = self.client[TEST_DB]
        created = set(db.list_collection_names())
        self.assertEqual(
            set(STORE_TYPES) & created, set(STORE_TYPES),
            f"expected setup to create all 7 store collections first, found: {sorted(created)}",
        )

        for t in ("invoices", "subscriptions"):
            result = subprocess.run(
                [sys.executable, STORE, "validate", t, "--json"],
                capture_output=True, text=True, env=self._env(),
            )
            self.assertEqual(
                result.returncode, 0,
                f"validate {t} failed: rc={result.returncode} stderr={result.stderr!r}",
            )
            parsed = json.loads(result.stdout)
            # positive: an empty non-conforming-ids list (a fresh, empty, validator-clean collection)
            self.assertEqual(parsed, [], f"expected `validate {t}` == [] after setup, got {parsed}")


if __name__ == "__main__":
    unittest.main()
