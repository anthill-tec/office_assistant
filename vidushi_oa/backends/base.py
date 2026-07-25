"""CR-OA-018 §S1 — the persistence backend interface.

A `Backend` abstracts the store operations the CLI needs so the engine can run on MongoDB
(the backend today) or embedded SQLite (§S2), selected by `VIDUSHI_BACKEND`. It yields a
`collection(type_)` handle exposing the collection operations `_cli.py` uses, plus db-level
provisioning. The domain logic (gen_id, transitions, sweeps, TOON output) sits ABOVE this seam
and is backend-agnostic.
"""
from abc import ABC, abstractmethod


class Backend(ABC):
    """Persistence backend contract. `name` identifies the backend (`mongo` | `sqlite`)."""

    name = "abstract"
    #: the exception type a duplicate-`id` insert raises for this backend
    dup_error = Exception

    @abstractmethod
    def check(self):
        """Readiness probe for `voa setup`. Return `(ok: bool, message: str)` — never raises."""

    @abstractmethod
    def collection(self, type_):
        """Return the store handle for collection `type_` — supports the collection
        operations `_cli.py` invokes (find/find_one/insert_one/replace_one/update_one/
        update_many/delete_one/count_documents/aggregate/create_index)."""

    @abstractmethod
    def db_name(self):
        """Human-readable name of the active datastore (for diagnostics)."""

    @abstractmethod
    def list_collections(self):
        """Names of the collections that currently exist."""

    @abstractmethod
    def provision(self, schemas):
        """Ensure each collection in `schemas` (type -> JSON Schema) exists and carries its
        `$jsonSchema` validator. Idempotent; returns the list of provisioned types."""
