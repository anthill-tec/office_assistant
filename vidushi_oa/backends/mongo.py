"""CR-OA-018 §S1 — MongoDB backend.

Wraps the pymongo access in `vidushi_oa._mongo`; `collection(type_)` returns a pymongo
Collection (which already exposes the required operations), and `provision` applies the
`$jsonSchema` validators. This is the only module that touches pymongo for data access.
"""
import os

from pymongo.errors import DuplicateKeyError

from vidushi_oa import _mongo
from vidushi_oa.backends import query as Q
from vidushi_oa.backends.base import Backend

_MONGO_OPS = {"ne": "$ne", "in": "$in", "lt": "$lt", "lte": "$lte",
              "gt": "$gt", "gte": "$gte", "exists": "$exists"}


def _cond_value(c):
    """Native mongo value for a Cond: bare for eq, else a `{$op: value}` document."""
    if c.op == "eq":
        return c.value
    return {_MONGO_OPS[c.op]: c.value}


def _elem_doc(conds):
    return {c.path: _cond_value(c) for c in conds}


def compile_query(node):
    """Compile a neutral query node to a native MongoDB query document."""
    if isinstance(node, Q.Cond):
        return {node.path: _cond_value(node)}
    if isinstance(node, Q.ElemMatch):
        return {node.path: {"$elemMatch": _elem_doc(node.conds)}}
    if isinstance(node, Q.Group):
        if node.kind == "all":
            parts = [compile_query(n) for n in node.nodes]
            merged, collide = {}, False
            for p in parts:
                for k, v in p.items():
                    if k in merged:
                        collide = True
                    merged[k] = v
            return {"$and": parts} if collide else merged
        if node.kind == "any":
            return {"$or": [compile_query(n) for n in node.nodes]}
        if node.kind == "none":
            if len(node.nodes) == 1 and isinstance(node.nodes[0], Q.ElemMatch):
                em = node.nodes[0]
                return {em.path: {"$not": {"$elemMatch": _elem_doc(em.conds)}}}
            return {"$nor": [compile_query(n) for n in node.nodes]}
    raise TypeError(f"not a query node: {node!r}")


def compile_update(upd):
    """Compile a neutral Update to a native MongoDB update document. A `resolve` also carries a
    `_filter` (the array `$elemMatch`) the caller must AND into the update's query for the
    positional `$` to bind."""
    doc = {}
    if upd.set:
        doc["$set"] = dict(upd.set)
    if upd.push:
        doc["$push"] = {field: {"$each": list(items)} for field, items in upd.push.items()}
    if upd.resolve:
        array_path, match_conds, set_fields = upd.resolve
        doc["_filter"] = {array_path: {"$elemMatch": _elem_doc(match_conds)}}
        doc.setdefault("$set", {}).update({f"{array_path}.$.{k}": v for k, v in set_fields.items()})
    return doc


class MongoBackend(Backend):
    name = "mongo"
    dup_error = DuplicateKeyError

    def _db(self):
        return _mongo.db()

    def check(self):
        from pymongo import MongoClient
        from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError

        uri = os.environ.get("VIDUSHI_MONGO_URI", "mongodb://127.0.0.1:27017")
        db_name = os.environ.get("VIDUSHI_MONGO_DB", "vidushi_oa")
        probe = MongoClient(uri, serverSelectionTimeoutMS=2000)
        try:
            probe.admin.command("ping")
        except (ServerSelectionTimeoutError, ConnectionFailure) as e:
            return (False,
                    f"Cannot reach MongoDB at {uri}: {e}\n"
                    f"Start a local mongod (default port 27017) — e.g. via your service manager "
                    f"(`systemctl start mongod`) or `mongod --dbpath <dir>` — then retry. "
                    f"To point elsewhere set VIDUSHI_MONGO_URI (e.g. "
                    f"VIDUSHI_MONGO_URI=mongodb://host:27017).")
        finally:
            probe.close()
        return (True, f"Mongo reachable at {uri} — db {db_name}")

    def collection(self, type_):
        return _mongo.coll(type_)

    def db_name(self):
        return self._db().name

    def list_collections(self):
        return self._db().list_collection_names()

    def provision(self, schemas):
        db = self._db()
        existing = set(self.list_collections())
        for type_, schema in schemas.items():
            if type_ not in existing:
                db.create_collection(type_)
                existing.add(type_)
            db.command("collMod", type_, validator={"$jsonSchema": schema},
                       validationLevel="moderate", validationAction="error")
        return list(schemas)
