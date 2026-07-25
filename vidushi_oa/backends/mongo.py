"""CR-OA-018 §S1 — MongoDB backend.

Wraps the pymongo access in `vidushi_oa._mongo`; `collection(type_)` returns a pymongo
Collection (which already exposes the required operations), and `provision` applies the
`$jsonSchema` validators. This is the only module that touches pymongo for data access.
"""
from vidushi_oa import _mongo
from vidushi_oa.backends.base import Backend


class MongoBackend(Backend):
    name = "mongo"

    def _db(self):
        return _mongo.db()

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
