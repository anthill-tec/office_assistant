"""CR-OA-018 §S1 — MongoDB backend.

Wraps the pymongo access in `vidushi_oa._mongo`; `collection(type_)` returns a pymongo
Collection (which already exposes the required operations), and `provision` applies the
`$jsonSchema` validators. This is the only module that touches pymongo for data access.
"""
import os

from pymongo.errors import DuplicateKeyError

from vidushi_oa import _mongo
from vidushi_oa.backends.base import Backend


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
