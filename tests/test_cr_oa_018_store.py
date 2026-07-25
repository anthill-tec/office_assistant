"""CR-OA-018 §S1/§S2 — the neutral `Store` contract + `MongoStore` (RED).

The neutral query/update model (`vidushi_oa/backends/query.py`) and the Mongo native
compiler (`vidushi_oa/backends/mongo.py`) already exist and are locked by
`test_cr_oa_018_neutral_query.py`. This file locks the layer ABOVE the compiler: a
`Store` abstraction (`vidushi_oa/backends/base.py`) that the CLI drives with the neutral
model instead of a raw pymongo Collection, plus a concrete `MongoStore`
(`vidushi_oa/backends/mongo.py`) and `Backend.store(type_)` (`MongoBackend.store`) that
returns one.

None of `Store`, `MongoStore`, or `Backend.store`/`MongoBackend.store` exist yet, so
EVERY test here is RED: most fail in `setUp` with an `AttributeError` on
`get_backend("mongo").store(...)` (no `store` attribute on `MongoBackend`); a few that
also exercise a not-yet-supported query op (`contains`) would additionally fail inside
`vidushi_oa.backends.query.cond` with a `ValueError` (`contains` not yet in `OPS`) once
`store()` exists — but today they never get that far because `setUp` fails first.

Runs against the throwaway Mongo DB `vidushi_oa_test` (never the real `vidushi_oa` DB).
Requires a local mongod on 127.0.0.1:27017 (the office_assistant instance, CR-OA-001).
"""
import os
import shutil
import tempfile
import unittest

import pymongo

from vidushi_oa.backends.query import ALL, Update, all_, cond, elem, none_

TEST_DB = "vidushi_oa_test"


class MongoStoreTestBase(unittest.TestCase):
    """Shared throwaway-DB fixture: mirrors tests/test_cr_oa_017_axi_conformance.py."""

    def setUp(self):
        self.data_dir = tempfile.mkdtemp(prefix="oa-cr018-store-")
        self._saved_env = {
            k: os.environ.get(k) for k in ("VIDUSHI_MONGO_DB", "VIDUSHI_DATA_DIR")
        }
        os.environ["VIDUSHI_MONGO_DB"] = TEST_DB
        os.environ["VIDUSHI_DATA_DIR"] = self.data_dir

        self.client = pymongo.MongoClient("mongodb://127.0.0.1:27017", serverSelectionTimeoutMS=2000)
        self.db = self.client[TEST_DB]
        self.raw = self.db["subscriptions"]

        from vidushi_oa.backends import get_backend
        self.backend = get_backend("mongo")
        self.store = self.backend.store("subscriptions")
        # §S2 AC: "ensure_id_index() makes a duplicate insert raise dup_error" — exercised
        # for every test via this shared setUp, plus a dedicated test below that proves the
        # BEFORE/AFTER effect of calling it explicitly on a second collection.
        self.store.ensure_id_index()

    def tearDown(self):
        self.client.drop_database(TEST_DB)
        self.client.close()
        shutil.rmtree(self.data_dir, ignore_errors=True)
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class StoreTypeTest(MongoStoreTestBase):
    """§S1 — `Backend.store(type_)` returns an instance of the abstract `Store`."""

    def test_backend_store_returns_a_store_instance(self):
        from vidushi_oa.backends.base import Store
        self.assertIsInstance(self.store, Store)


class InsertFindTest(MongoStoreTestBase):
    def test_insert_then_find_all_returns_it(self):
        self.store.insert({"id": "sub_ins", "provider": "Insertco", "status": "NEW"})

        docs = self.store.find(ALL)

        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["id"], "sub_ins")
        self.assertEqual(docs[0]["provider"], "Insertco")
        self.assertEqual(docs[0]["status"], "NEW")
        self.assertNotIn("_id", docs[0], "find(ALL) must strip the mongo _id")

    def test_insert_duplicate_id_raises_dup_error(self):
        self.store.insert({"id": "sub_dup", "status": "NEW"})

        with self.assertRaises(self.backend.dup_error):
            self.store.insert({"id": "sub_dup", "status": "NEW"})

        # negative bound: only the first insert landed
        self.assertEqual(self.raw.count_documents({"id": "sub_dup"}), 1)

    def test_find_all_returns_full_docs_sans_mongo_id_for_multiple_rows(self):
        self.store.insert({"id": "sub_m1", "provider": "One", "status": "NEW"})
        self.store.insert({"id": "sub_m2", "provider": "Two", "status": "DUE"})

        docs = self.store.find(ALL)

        self.assertEqual(len(docs), 2)
        ids = sorted(d["id"] for d in docs)
        self.assertEqual(ids, ["sub_m1", "sub_m2"])
        for d in docs:
            self.assertNotIn("_id", d)


class FindProjectionTest(MongoStoreTestBase):
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
        self.assertNotIn("_id", d)
        self.assertNotIn("id", d, "projection must not silently add fields beyond what was requested")
        self.assertNotIn("plan", d)


class FindOneTest(MongoStoreTestBase):
    def test_find_one_returns_matching_doc(self):
        self.store.insert({"id": "sub_fo", "provider": "FindOneCo", "status": "NEW"})

        doc = self.store.find_one(cond("id", "eq", "sub_fo"))

        self.assertIsNotNone(doc)
        self.assertEqual(doc["id"], "sub_fo")
        self.assertEqual(doc["provider"], "FindOneCo")
        self.assertNotIn("_id", doc)

    def test_find_one_returns_none_when_no_match(self):
        doc = self.store.find_one(cond("id", "eq", "sub_missing_xyz"))
        self.assertIsNone(doc)


class FindOperatorTest(MongoStoreTestBase):
    def setUp(self):
        super().setUp()
        self.store.insert({"id": "sub_new", "provider": "NewCo", "status": "NEW", "renews": "2026-06-01"})
        self.store.insert({"id": "sub_due", "provider": "DueCo", "status": "DUE", "renews": "2026-07-15"})
        self.store.insert({"id": "sub_ip", "provider": "IpCo", "status": "IN_PROGRESS", "renews": "2026-08-20"})
        self.store.insert({"id": "sub_done", "provider": "DoneCo", "status": "COMPLETED", "renews": "2026-09-01"})

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

    def test_eq_none_matches_null_value_and_missing_key(self):
        self.store.insert({"id": "sub_null_val", "provider": "HasNull", "status": "NEW", "disposition": None})
        self.store.insert({"id": "sub_null_missing", "provider": "MissingField", "status": "NEW"})
        self.store.insert({"id": "sub_has_value", "provider": "HasValue", "status": "NEW", "disposition": "KEEP"})

        docs = self.store.find(cond("disposition", "eq", None))
        ids = sorted(d["id"] for d in docs)

        self.assertEqual(ids, ["sub_null_missing", "sub_null_val"])
        self.assertNotIn("sub_has_value", ids)
        self.assertEqual(len(docs), 2)


class ElemMatchTest(MongoStoreTestBase):
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


class UpdateTest(MongoStoreTestBase):
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


class DeleteTest(MongoStoreTestBase):
    def test_delete_removes_matching_row_and_returns_count(self):
        self.store.insert({"id": "sub_del", "status": "NEW"})

        count = self.store.delete(cond("id", "eq", "sub_del"))

        self.assertEqual(count, 1)
        self.assertIsNone(self.store.find_one(cond("id", "eq", "sub_del")))
        # negative bound: deleting again matches nothing
        self.assertEqual(self.store.delete(cond("id", "eq", "sub_del")), 0)


class CountTest(MongoStoreTestBase):
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


class NonconformingTest(MongoStoreTestBase):
    def test_nonconforming_returns_ids_violating_the_schema(self):
        schema = {
            "bsonType": "object",
            "properties": {"status": {"enum": ["NEW", "IN_PROGRESS", "DUE"]}},
        }
        # seed directly via pymongo, bypassing any client-side check `insert()` might do
        self.raw.insert_many([
            {"id": "sub_good", "status": "NEW"},
            {"id": "sub_bad1", "status": "BOGUS"},
            {"id": "sub_bad2", "status": "WOMBAT"},
        ])

        ids = self.store.nonconforming(schema)

        self.assertEqual(sorted(ids), ["sub_bad1", "sub_bad2"])
        self.assertNotIn("sub_good", ids)


class EnsureIdIndexTest(MongoStoreTestBase):
    def test_ensure_id_index_makes_duplicate_insert_raise_dup_error(self):
        # a SEPARATE collection/store that has NOT had ensure_id_index() called yet
        cases_store = self.backend.store("cases")

        cases_store.insert({"id": "case_a", "status": "NEW"})
        # before ensure_id_index(): no unique constraint, a duplicate id is accepted
        cases_store.insert({"id": "case_a", "status": "NEW"})
        self.assertEqual(cases_store.count(cond("id", "eq", "case_a")), 2)

        cases_store.ensure_id_index()

        cases_store.insert({"id": "case_b", "status": "NEW"})
        with self.assertRaises(self.backend.dup_error):
            cases_store.insert({"id": "case_b", "status": "NEW"})
        # negative bound: only the first post-index insert of case_b landed
        self.assertEqual(cases_store.count(cond("id", "eq", "case_b")), 1)


if __name__ == "__main__":
    unittest.main()
