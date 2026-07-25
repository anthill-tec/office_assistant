"""CR-OA-018 §S1/§S2 — neutral query/update model + native Mongo compilation (RED).

The CLI builds a neutral, backend-agnostic model; each backend compiles it to its OWN native
query (no dialect-translation layer). This locks the model's builders and the MongoBackend
compiler for the exact query shapes `_cli.py` uses (equality/range/in/ne, elem_match, any=OR,
none-of-elem_match, updates set/push/resolve). The SQLite compiler (§S2) compiles the same
model to SQL/JSON1 and is parity-tested against Mongo separately.
"""
import unittest


class NeutralModelTest(unittest.TestCase):
    def test_builders(self):
        from vidushi_oa.backends import query as Q

        c = Q.cond("status", "eq", "DUE")
        self.assertEqual((c.path, c.op, c.value), ("status", "eq", "DUE"))
        g = Q.all_(Q.cond("a", "eq", 1), Q.cond("b", "lt", 2))
        self.assertEqual(g.kind, "all")
        self.assertEqual(len(g.nodes), 2)
        em = Q.elem("actions", Q.cond("status", "eq", "OPEN"))
        self.assertEqual(em.path, "actions")


class MongoCompileTest(unittest.TestCase):
    def _c(self, node):
        from vidushi_oa.backends.mongo import compile_query
        return compile_query(node)

    def test_empty_group_matches_all(self):
        from vidushi_oa.backends import query as Q
        self.assertEqual(self._c(Q.ALL), {})

    def test_and_of_eq_and_range(self):
        from vidushi_oa.backends import query as Q
        q = Q.all_(Q.cond("status", "eq", "DUE"), Q.cond("expiry", "lt", "2026-01-01"))
        self.assertEqual(self._c(q), {"status": "DUE", "expiry": {"$lt": "2026-01-01"}})

    def test_ne_and_in(self):
        from vidushi_oa.backends import query as Q
        q = Q.all_(Q.cond("status", "in", ["NEW", "IN_PROGRESS"]), Q.cond("status", "ne", "DUE"))
        # distinct-key merge would collide on 'status'; compiler must fall back to $and
        got = self._c(q)
        self.assertIn("$and", got)

    def test_any_is_or(self):
        from vidushi_oa.backends import query as Q
        q = Q.any_(Q.cond("renews", "lte", "x"), Q.cond("expiry", "lte", "x"))
        self.assertEqual(self._c(q), {"$or": [{"renews": {"$lte": "x"}}, {"expiry": {"$lte": "x"}}]})

    def test_elem_match(self):
        from vidushi_oa.backends import query as Q
        q = Q.elem("actions", Q.cond("action", "eq", "stuck-chase"), Q.cond("status", "eq", "OPEN"))
        self.assertEqual(self._c(q), {"actions": {"$elemMatch": {"action": "stuck-chase", "status": "OPEN"}}})

    def test_none_of_elem_match_is_not(self):
        from vidushi_oa.backends import query as Q
        q = Q.none_(Q.elem("actions", Q.cond("action", "eq", "stuck-chase"), Q.cond("status", "eq", "OPEN")))
        self.assertEqual(
            self._c(q),
            {"actions": {"$not": {"$elemMatch": {"action": "stuck-chase", "status": "OPEN"}}}},
        )


class MongoUpdateCompileTest(unittest.TestCase):
    def _u(self, upd):
        from vidushi_oa.backends.mongo import compile_update
        return compile_update(upd)

    def test_set_and_push(self):
        from vidushi_oa.backends import query as Q
        u = Q.Update(set={"status": "IN_PROGRESS", "updated": "d"}, push={"actions": [{"action": "x"}]})
        self.assertEqual(
            self._u(u),
            {"$set": {"status": "IN_PROGRESS", "updated": "d"}, "$push": {"actions": {"$each": [{"action": "x"}]}}},
        )

    def test_resolve_positional(self):
        from vidushi_oa.backends import query as Q
        u = Q.Update(resolve=("actions", (Q.cond("action", "eq", "x"), Q.cond("status", "eq", "OPEN")),
                              {"status": "RESOLVED", "resolved": "d"}))
        # a resolve compiles to an $elemMatch arrayFilters-free positional $ update in mongo
        got = self._u(u)
        self.assertEqual(got["_filter"], {"actions": {"$elemMatch": {"action": "x", "status": "OPEN"}}})
        self.assertEqual(got["$set"], {"actions.$.status": "RESOLVED", "actions.$.resolved": "d"})


if __name__ == "__main__":
    unittest.main()
