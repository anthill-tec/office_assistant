"""CR-OA-018 §S2 — `SqliteBackend`/`SqliteStore` (RED).

The SQLite backend (`vidushi_oa/backends/sqlite.py` or similar — module path is the
GREEN agent's choice) does not exist yet: `get_backend("sqlite")` raises `ValueError`
today (`sqlite` is not in `_BACKENDS`, `vidushi_oa/backends/__init__.py`). Every test
here is RED for that reason.

Intended design under test (per the dispatch spec):
  - `get_backend("sqlite")` -> a backend with `.name == "sqlite"`, `.dup_error`
    (`sqlite3.IntegrityError`), `.store(type_)` returning a `base.Store`.
  - Config via env: `VIDUSHI_BACKEND=sqlite` selects it; `VIDUSHI_SQLITE_PATH` points at
    the db file. Storage: one table per type `(id TEXT PRIMARY KEY, doc JSON)`.
  - `get_backend("sqlite").provision({type: schema})` creates the tables and registers
    the JSON Schemas used for write validation (the `jsonschema` package, per the CR's
    §S2 design — not yet a project dependency, so this file relies on it only THROUGH
    the not-yet-existing SqliteStore, never imports it directly).
  - Write validation parity: `store.insert(bad_doc)` violating the type's schema RAISES
    (parity with Mongo's `$jsonSchema`); no row lands. A conforming insert succeeds.

Mirrors the operations covered for the Mongo `Store` in `tests/test_cr_oa_018_store.py`,
plus a cross-backend `ParityTest` proving the two native compilers (Mongo query docs vs
SQLite JSON1) agree on the same neutral query. The parity test requires a local mongod
on 127.0.0.1:27017 (the office_assistant instance, CR-OA-001) and uses the throwaway
Mongo DB `vidushi_oa_test_parity` (never the real `vidushi_oa` DB).
"""
import os
import shutil
import sqlite3
import tempfile
import unittest

import pymongo

from vidushi_oa.backends.query import ALL, Update, all_, cond, elem, none_


class SqliteStoreTestBase(unittest.TestCase):
    """Shared throwaway-sqlite-file fixture: a fresh db file per test, `subscriptions`
    table pre-provisioned via `ensure_id_index()` (no schema validator registered, so
    `insert()` has nothing to reject here — schema validation is exercised separately
    by `WriteValidationParityTest`)."""

    def setUp(self):
        self.data_dir = tempfile.mkdtemp(prefix="oa-cr018-sqlite-")
        self.db_path = os.path.join(self.data_dir, "oa.db")
        self._saved_env = {
            k: os.environ.get(k) for k in ("VIDUSHI_BACKEND", "VIDUSHI_SQLITE_PATH")
        }
        os.environ["VIDUSHI_BACKEND"] = "sqlite"
        os.environ["VIDUSHI_SQLITE_PATH"] = self.db_path

        from vidushi_oa.backends import get_backend
        self.backend = get_backend("sqlite")
        self.store = self.backend.store("subscriptions")
        self.store.ensure_id_index()

    def tearDown(self):
        shutil.rmtree(self.data_dir, ignore_errors=True)
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _raw_count(self, type_, id_):
        """Row count straight from sqlite, bypassing the Store — for negative-bound
        assertions that a rejected/duplicate insert truly did not land."""
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(f"SELECT COUNT(*) FROM {type_} WHERE id = ?", (id_,)).fetchone()
            return row[0]
        finally:
            conn.close()


class BackendResolutionTest(unittest.TestCase):
    """§S2 AC — `get_backend("sqlite")` resolves to a concrete backend by name and via
    `VIDUSHI_BACKEND`, with the right identity/dup_error/store-type."""

    def setUp(self):
        self.data_dir = tempfile.mkdtemp(prefix="oa-cr018-sqlite-resolve-")
        self.db_path = os.path.join(self.data_dir, "resolve.db")
        self._saved_env = {
            k: os.environ.get(k) for k in ("VIDUSHI_BACKEND", "VIDUSHI_SQLITE_PATH")
        }
        os.environ["VIDUSHI_SQLITE_PATH"] = self.db_path
        os.environ.pop("VIDUSHI_BACKEND", None)

    def tearDown(self):
        shutil.rmtree(self.data_dir, ignore_errors=True)
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_get_backend_sqlite_by_name_has_sqlite_identity(self):
        from vidushi_oa.backends import get_backend
        backend = get_backend("sqlite")
        self.assertEqual(backend.name, "sqlite")

    def test_get_backend_sqlite_dup_error_is_sqlite3_integrity_error(self):
        from vidushi_oa.backends import get_backend
        backend = get_backend("sqlite")
        self.assertIs(backend.dup_error, sqlite3.IntegrityError)

    def test_get_backend_sqlite_store_returns_a_store_instance(self):
        from vidushi_oa.backends import get_backend
        from vidushi_oa.backends.base import Store
        backend = get_backend("sqlite")
        store = backend.store("subscriptions")
        self.assertIsInstance(store, Store)

    def test_vidushi_backend_env_var_sqlite_selects_sqlite_backend(self):
        os.environ["VIDUSHI_BACKEND"] = "sqlite"
        from vidushi_oa.backends import get_backend
        backend = get_backend()
        self.assertEqual(backend.name, "sqlite")


class InsertFindTest(SqliteStoreTestBase):
    def test_insert_then_find_all_returns_it(self):
        self.store.insert({"id": "sub_ins", "provider": "Insertco", "status": "NEW"})

        docs = self.store.find(ALL)

        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["id"], "sub_ins")
        self.assertEqual(docs[0]["provider"], "Insertco")
        self.assertEqual(docs[0]["status"], "NEW")

    def test_insert_duplicate_id_raises_dup_error(self):
        self.store.insert({"id": "sub_dup", "status": "NEW"})

        with self.assertRaises(self.backend.dup_error):
            self.store.insert({"id": "sub_dup", "status": "NEW"})

        # negative bound: only the first insert landed
        self.assertEqual(self._raw_count("subscriptions", "sub_dup"), 1)

    def test_find_all_returns_full_docs_for_multiple_rows(self):
        self.store.insert({"id": "sub_m1", "provider": "One", "status": "NEW"})
        self.store.insert({"id": "sub_m2", "provider": "Two", "status": "DUE"})

        docs = self.store.find(ALL)

        self.assertEqual(len(docs), 2)
        ids = sorted(d["id"] for d in docs)
        self.assertEqual(ids, ["sub_m1", "sub_m2"])


class FindProjectionTest(SqliteStoreTestBase):
    def test_find_with_fields_projects_only_requested_fields(self):
        self.store.insert({
            "id": "sub_proj", "provider": "Foo", "status": "NEW",
            "plan": "premium", "currency": "INR",
        })

        docs = self.store.find(cond("id", "eq", "sub_proj"), fields=["provider", "status"])

        self.assertEqual(len(docs), 1)
        d = docs[0]
        self.assertEqual(set(d.keys()), {"provider", "status"})
        self.assertEqual(d["provider"], "Foo")
        self.assertEqual(d["status"], "NEW")
        self.assertNotIn("id", d, "projection must not silently add fields beyond what was requested")
        self.assertNotIn("plan", d)


class FindOneTest(SqliteStoreTestBase):
    def test_find_one_returns_matching_doc(self):
        self.store.insert({"id": "sub_fo", "provider": "FindOneCo", "status": "NEW"})

        doc = self.store.find_one(cond("id", "eq", "sub_fo"))

        self.assertIsNotNone(doc)
        self.assertEqual(doc["id"], "sub_fo")
        self.assertEqual(doc["provider"], "FindOneCo")

    def test_find_one_returns_none_when_no_match(self):
        doc = self.store.find_one(cond("id", "eq", "sub_missing_xyz"))
        self.assertIsNone(doc)


class FindOperatorTest(SqliteStoreTestBase):
    def setUp(self):
        super().setUp()
        self.store.insert({"id": "sub_new", "provider": "NewCo", "status": "NEW", "renews": "2026-06-01", "disposition": "KEEP"})
        self.store.insert({"id": "sub_due", "provider": "DueCo", "status": "DUE", "renews": "2026-07-15", "disposition": "KEEP"})
        self.store.insert({"id": "sub_ip", "provider": "IpCo", "status": "IN_PROGRESS", "renews": "2026-08-20", "disposition": "KEEP"})
        self.store.insert({"id": "sub_done", "provider": "DoneCo", "status": "COMPLETED", "renews": "2026-09-01", "disposition": "KEEP"})

    def test_cond_in_matches_only_listed_statuses(self):
        docs = self.store.find(cond("status", "in", ["NEW", "DUE"]))
        ids = sorted(d["id"] for d in docs)
        self.assertEqual(ids, ["sub_due", "sub_new"])
        # negative bound: exactly 2, IN_PROGRESS/COMPLETED excluded
        self.assertEqual(len(docs), 2)

    def test_cond_ne_excludes_matching_status(self):
        docs = self.store.find(cond("status", "ne", "DUE"))
        ids = sorted(d["id"] for d in docs)
        self.assertEqual(ids, ["sub_done", "sub_ip", "sub_new"])
        self.assertEqual(len(docs), 3)

    def test_range_all_gte_lte_matches_inclusive_window(self):
        q = all_(cond("renews", "gte", "2026-07-01"), cond("renews", "lte", "2026-08-31"))
        docs = self.store.find(q)
        ids = sorted(d["id"] for d in docs)
        self.assertEqual(ids, ["sub_due", "sub_ip"])
        self.assertEqual(len(docs), 2)

    def test_contains_matches_case_insensitive_substring(self):
        self.store.insert({"id": "sub_cs1", "provider": "SubStack", "status": "NEW"})
        self.store.insert({"id": "sub_cs2", "provider": "AMAZON SUBSCRIPTION", "status": "NEW"})

        docs = self.store.find(cond("provider", "contains", "sub"))
        ids = sorted(d["id"] for d in docs)

        self.assertEqual(ids, ["sub_cs1", "sub_cs2"])
        # negative bound: providers without "sub" (case-insensitive) are excluded
        self.assertNotIn("sub_new", ids)
        self.assertNotIn("sub_due", ids)
        self.assertEqual(len(docs), 2)

    def test_contains_treats_like_wildcards_as_literals(self):
        # `%` and `_` are LIKE metacharacters; `contains` must match them literally
        # (parity with Mongo's re.escape substring), not as wildcards.
        self.store.insert({"id": "sub_pct", "provider": "100% cotton", "status": "NEW"})
        self.store.insert({"id": "sub_plain", "provider": "100 percent", "status": "NEW"})
        self.store.insert({"id": "sub_us", "provider": "foo_bar", "status": "NEW"})
        self.store.insert({"id": "sub_usx", "provider": "fooXbar", "status": "NEW"})

        pct = sorted(d["id"] for d in self.store.find(cond("provider", "contains", "100%")))
        self.assertEqual(pct, ["sub_pct"])

        us = sorted(d["id"] for d in self.store.find(cond("provider", "contains", "foo_bar")))
        self.assertEqual(us, ["sub_us"])

    def test_eq_none_matches_null_value_and_missing_key(self):
        self.store.insert({"id": "sub_null_val", "provider": "HasNull", "status": "NEW", "disposition": None})
        self.store.insert({"id": "sub_null_missing", "provider": "MissingField", "status": "NEW"})
        self.store.insert({"id": "sub_has_value", "provider": "HasValue", "status": "NEW", "disposition": "KEEP"})

        docs = self.store.find(cond("disposition", "eq", None))
        ids = sorted(d["id"] for d in docs)

        self.assertEqual(ids, ["sub_null_missing", "sub_null_val"])
        self.assertNotIn("sub_has_value", ids)
        self.assertEqual(len(docs), 2)


class ElemMatchTest(SqliteStoreTestBase):
    def test_elem_match_returns_rows_with_matching_array_element(self):
        self.store.insert({"id": "sub_act1", "status": "NEW", "actions": [{"action": "x", "status": "OPEN"}]})
        self.store.insert({"id": "sub_act2", "status": "NEW", "actions": [{"action": "y", "status": "RESOLVED"}]})
        self.store.insert({"id": "sub_act3", "status": "NEW", "actions": []})

        docs = self.store.find(elem("actions", cond("status", "eq", "OPEN")))
        ids = sorted(d["id"] for d in docs)

        self.assertEqual(ids, ["sub_act1"])
        self.assertEqual(len(docs), 1)

    def test_none_of_elem_match_excludes_rows_with_matching_open_action(self):
        self.store.insert({"id": "sub_stuck1", "status": "NEW",
                            "actions": [{"action": "stuck-chase", "status": "OPEN"}]})
        self.store.insert({"id": "sub_stuck2", "status": "NEW",
                            "actions": [{"action": "stuck-chase", "status": "RESOLVED"}]})
        self.store.insert({"id": "sub_stuck3", "status": "NEW",
                            "actions": [{"action": "other", "status": "OPEN"}]})
        self.store.insert({"id": "sub_stuck4", "status": "NEW", "actions": []})

        q = none_(elem("actions", cond("action", "eq", "stuck-chase"), cond("status", "eq", "OPEN")))
        docs = self.store.find(q)
        ids = sorted(d["id"] for d in docs)

        self.assertEqual(ids, ["sub_stuck2", "sub_stuck3", "sub_stuck4"])
        # negative bound: the row with the OPEN stuck-chase action is excluded
        self.assertNotIn("sub_stuck1", ids)
        self.assertEqual(len(docs), 3)


class UpdateTest(SqliteStoreTestBase):
    def test_update_set_and_push_applies_and_returns_matched_count(self):
        self.store.insert({"id": "sub_upd", "status": "NEW", "provider": "UpdCo"})

        matched = self.store.update(
            cond("id", "eq", "sub_upd"),
            Update(set={"status": "IN_PROGRESS"}, push={"actions": [{"action": "chase", "status": "OPEN"}]}),
        )

        self.assertEqual(matched, 1)
        doc = self.store.find_one(cond("id", "eq", "sub_upd"))
        self.assertEqual(doc["status"], "IN_PROGRESS")
        self.assertEqual(doc["actions"], [{"action": "chase", "status": "OPEN"}])

    def test_update_on_no_match_returns_zero_and_writes_nothing(self):
        matched = self.store.update(cond("id", "eq", "sub_missing_xyz"), Update(set={"status": "IN_PROGRESS"}))
        self.assertEqual(matched, 0)
        self.assertIsNone(self.store.find_one(cond("id", "eq", "sub_missing_xyz")))

    def test_update_resolve_flips_only_the_first_matching_action(self):
        self.store.insert({
            "id": "sub_res", "status": "NEW",
            "actions": [{"action": "a", "status": "OPEN"}, {"action": "b", "status": "OPEN"}],
        })

        matched = self.store.update(
            cond("id", "eq", "sub_res"),
            Update(resolve=("actions", (cond("action", "eq", "a"), cond("status", "eq", "OPEN")),
                             {"status": "RESOLVED"})),
        )

        self.assertEqual(matched, 1)
        doc = self.store.find_one(cond("id", "eq", "sub_res"))
        actions_by_name = {a["action"]: a["status"] for a in doc["actions"]}
        self.assertEqual(actions_by_name["a"], "RESOLVED")
        # negative bound: the OTHER open action ("b") is untouched by the positional update
        self.assertEqual(actions_by_name["b"], "OPEN")


class DeleteTest(SqliteStoreTestBase):
    def test_delete_removes_matching_row_and_returns_count(self):
        self.store.insert({"id": "sub_del", "status": "NEW"})

        count = self.store.delete(cond("id", "eq", "sub_del"))

        self.assertEqual(count, 1)
        self.assertIsNone(self.store.find_one(cond("id", "eq", "sub_del")))
        # negative bound: deleting again matches nothing
        self.assertEqual(self.store.delete(cond("id", "eq", "sub_del")), 0)


class CountTest(SqliteStoreTestBase):
    def setUp(self):
        super().setUp()
        self.store.insert({"id": "sub_c1", "status": "NEW"})
        self.store.insert({"id": "sub_c2", "status": "NEW"})
        self.store.insert({"id": "sub_c3", "status": "NEW"})
        self.store.insert({"id": "sub_c4", "status": "DUE"})
        self.store.insert({"id": "sub_c5", "status": "DUE"})

    def test_count_matches_query(self):
        self.assertEqual(self.store.count(ALL), 5)
        self.assertEqual(self.store.count(cond("status", "eq", "NEW")), 3)
        self.assertEqual(self.store.count(cond("status", "eq", "DUE")), 2)
        # negative bound: an unmatched status counts to zero
        self.assertEqual(self.store.count(cond("status", "eq", "IN_PROGRESS")), 0)

    def test_count_by_field_returns_value_to_count_dict(self):
        counts = self.store.count_by("status")
        self.assertEqual(counts, {"NEW": 3, "DUE": 2})


class NonconformingTest(SqliteStoreTestBase):
    def test_nonconforming_returns_ids_violating_the_schema(self):
        schema = {
            "type": "object",
            "properties": {"status": {"enum": ["NEW", "IN_PROGRESS", "DUE"]}},
        }
        # seed directly via the store — no schema was provisioned in setUp for this
        # type, so insert() has nothing to reject here (mirrors the Mongo test's raw
        # pymongo insert_many bypassing the validator).
        self.store.insert({"id": "sub_good", "status": "NEW"})
        self.store.insert({"id": "sub_bad1", "status": "BOGUS"})
        self.store.insert({"id": "sub_bad2", "status": "WOMBAT"})

        ids = self.store.nonconforming(schema)

        self.assertEqual(sorted(ids), ["sub_bad1", "sub_bad2"])
        self.assertNotIn("sub_good", ids)


class EnsureIdIndexTest(unittest.TestCase):
    """§S2 AC — `ensure_id_index()` provisions the table so a duplicate `id` insert
    raises `dup_error`, and is idempotent (repeat calls don't drop existing rows)."""

    def setUp(self):
        self.data_dir = tempfile.mkdtemp(prefix="oa-cr018-sqlite-index-")
        self.db_path = os.path.join(self.data_dir, "index.db")
        self._saved_env = {
            k: os.environ.get(k) for k in ("VIDUSHI_BACKEND", "VIDUSHI_SQLITE_PATH")
        }
        os.environ["VIDUSHI_BACKEND"] = "sqlite"
        os.environ["VIDUSHI_SQLITE_PATH"] = self.db_path

        from vidushi_oa.backends import get_backend
        self.backend = get_backend("sqlite")

    def tearDown(self):
        shutil.rmtree(self.data_dir, ignore_errors=True)
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_ensure_id_index_makes_duplicate_insert_raise_dup_error(self):
        cases_store = self.backend.store("cases")
        cases_store.ensure_id_index()

        cases_store.insert({"id": "case_b", "status": "NEW"})
        with self.assertRaises(self.backend.dup_error):
            cases_store.insert({"id": "case_b", "status": "NEW"})

        # negative bound: only the first insert of case_b landed
        self.assertEqual(cases_store.count(cond("id", "eq", "case_b")), 1)

    def test_ensure_id_index_is_idempotent_and_preserves_existing_rows(self):
        cases_store = self.backend.store("cases")
        cases_store.ensure_id_index()
        cases_store.insert({"id": "case_idem", "status": "NEW"})

        cases_store.ensure_id_index()  # calling again must not drop existing data

        self.assertEqual(cases_store.count(cond("id", "eq", "case_idem")), 1)
        doc = cases_store.find_one(cond("id", "eq", "case_idem"))
        self.assertEqual(doc["status"], "NEW")


class WriteValidationParityTest(unittest.TestCase):
    """§S2 AC — write validation parity: a schema-violating insert raises and writes
    nothing; a conforming insert lands. `provision()` registers the schema used."""

    def setUp(self):
        self.data_dir = tempfile.mkdtemp(prefix="oa-cr018-sqlite-validate-")
        self.db_path = os.path.join(self.data_dir, "validate.db")
        self._saved_env = {
            k: os.environ.get(k) for k in ("VIDUSHI_BACKEND", "VIDUSHI_SQLITE_PATH")
        }
        os.environ["VIDUSHI_BACKEND"] = "sqlite"
        os.environ["VIDUSHI_SQLITE_PATH"] = self.db_path

        from vidushi_oa.backends import get_backend
        self.backend = get_backend("sqlite")
        self.schema = {
            "type": "object",
            "properties": {"status": {"enum": ["NEW", "IN_PROGRESS", "COMPLETED"]}},
        }
        self.backend.provision({"subscriptions": self.schema})
        self.store = self.backend.store("subscriptions")

    def tearDown(self):
        shutil.rmtree(self.data_dir, ignore_errors=True)
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _raw_count(self, id_):
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute("SELECT COUNT(*) FROM subscriptions WHERE id = ?", (id_,)).fetchone()
            return row[0]
        finally:
            conn.close()

    def test_insert_violating_status_enum_raises_and_writes_nothing(self):
        with self.assertRaises(Exception):
            self.store.insert({"id": "sub_bogus", "status": "bogus"})

        self.assertIsNone(self.store.find_one(cond("id", "eq", "sub_bogus")))
        # negative bound: the rejected row never reaches the table, straight from sqlite
        self.assertEqual(self._raw_count("sub_bogus"), 0)

    def test_insert_conforming_to_schema_lands(self):
        self.store.insert({"id": "sub_ok", "status": "NEW"})

        doc = self.store.find_one(cond("id", "eq", "sub_ok"))
        self.assertIsNotNone(doc)
        self.assertEqual(doc["status"], "NEW")
        self.assertEqual(self._raw_count("sub_ok"), 1)


PARITY_MONGO_DB = "vidushi_oa_test_parity"


class ParityTestBase(unittest.TestCase):
    """Seeds the SAME rows into both a throwaway Mongo store and a throwaway sqlite
    store, so a query run against each must return the identical result-id set —
    proving the two native compilers (mongo query docs vs sqlite JSON1) agree.

    Requires a local mongod on 127.0.0.1:27017 (the office_assistant instance,
    CR-OA-001). Uses `vidushi_oa_test_parity`, never the real `vidushi_oa` DB.
    """

    def setUp(self):
        self.data_dir = tempfile.mkdtemp(prefix="oa-cr018-sqlite-parity-")
        self.db_path = os.path.join(self.data_dir, "parity.db")
        self._saved_env = {
            k: os.environ.get(k) for k in ("VIDUSHI_MONGO_DB", "VIDUSHI_SQLITE_PATH")
        }
        os.environ["VIDUSHI_MONGO_DB"] = PARITY_MONGO_DB
        os.environ["VIDUSHI_SQLITE_PATH"] = self.db_path

        self.mongo_client = pymongo.MongoClient("mongodb://127.0.0.1:27017", serverSelectionTimeoutMS=2000)

        from vidushi_oa.backends import get_backend
        self.mongo_store = get_backend("mongo").store("subscriptions")
        self.mongo_store.ensure_id_index()
        self.sqlite_store = get_backend("sqlite").store("subscriptions")
        self.sqlite_store.ensure_id_index()

        rows = [
            {"id": "sub_p1", "provider": "Alpha", "status": "NEW", "renews": "2026-06-01",
             "disposition": None, "actions": [{"action": "chase", "status": "OPEN"}]},
            {"id": "sub_p2", "provider": "SubscriptionCo", "status": "DUE", "renews": "2026-07-15",
             "disposition": "KEEP", "actions": [{"action": "chase", "status": "RESOLVED"}]},
            {"id": "sub_p3", "provider": "Gamma", "status": "IN_PROGRESS", "renews": "2026-08-20",
             "disposition": "KEEP", "actions": []},
            {"id": "sub_p4", "provider": "Delta Sub", "status": "COMPLETED", "renews": "2026-09-01",
             "disposition": None, "actions": [{"action": "other", "status": "OPEN"}]},
        ]
        for row in rows:
            self.mongo_store.insert(dict(row))
            self.sqlite_store.insert(dict(row))

    def tearDown(self):
        self.mongo_client.drop_database(PARITY_MONGO_DB)
        self.mongo_client.close()
        shutil.rmtree(self.data_dir, ignore_errors=True)
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _assert_same_ids(self, query):
        mongo_ids = sorted(d["id"] for d in self.mongo_store.find(query))
        sqlite_ids = sorted(d["id"] for d in self.sqlite_store.find(query))
        self.assertEqual(mongo_ids, sqlite_ids,
                          f"mongo/sqlite disagree: mongo={mongo_ids} sqlite={sqlite_ids}")
        return mongo_ids


class ParityTest(ParityTestBase):
    def test_elem_match_returns_identical_ids_across_backends(self):
        ids = self._assert_same_ids(elem("actions", cond("status", "eq", "OPEN")))
        self.assertEqual(ids, ["sub_p1", "sub_p4"])

    def test_none_of_elem_match_returns_identical_ids_across_backends(self):
        q = none_(elem("actions", cond("status", "eq", "OPEN")))
        ids = self._assert_same_ids(q)
        self.assertEqual(ids, ["sub_p2", "sub_p3"])

    def test_in_operator_returns_identical_ids_across_backends(self):
        ids = self._assert_same_ids(cond("status", "in", ["NEW", "DUE"]))
        self.assertEqual(ids, ["sub_p1", "sub_p2"])

    def test_eq_none_returns_identical_ids_across_backends(self):
        ids = self._assert_same_ids(cond("disposition", "eq", None))
        self.assertEqual(ids, ["sub_p1", "sub_p4"])

    def test_range_returns_identical_ids_across_backends(self):
        q = all_(cond("renews", "gte", "2026-07-01"), cond("renews", "lte", "2026-08-31"))
        ids = self._assert_same_ids(q)
        self.assertEqual(ids, ["sub_p2", "sub_p3"])


if __name__ == "__main__":
    unittest.main()
