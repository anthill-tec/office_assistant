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
    def store(self, type_):
        """Return the neutral `Store` for collection `type_` — the CLI drives it with the
        backend-agnostic query/update model instead of a raw collection handle."""

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


class Store(ABC):
    """Neutral per-collection store the CLI drives with the backend-agnostic query/update
    model (`vidushi_oa.backends.query`). Each backend supplies a concrete `Store` that
    compiles those nodes to its own native dialect."""

    @abstractmethod
    def find(self, query, fields=None, extra=None):
        """Return the list of docs matching `query` (a neutral query node), projecting
        `fields` (all fields when None) and stripping the backend's internal id. `extra` is
        an optional backend-native filter ANDed with the compiled query."""

    @abstractmethod
    def find_one(self, query, fields=None):
        """Return the first doc matching `query`, or None; projects like `find`."""

    @abstractmethod
    def insert(self, doc):
        """Insert `doc`. Raises the backend's `dup_error` on a duplicate `id`."""

    @abstractmethod
    def replace(self, id, doc):
        """Replace (upsert) the doc with the given `id` by `doc`."""

    @abstractmethod
    def update(self, query, update, many=False):
        """Apply the neutral `Update` to docs matching `query` (all matches when `many`,
        else the first). Return the number of matched docs."""

    @abstractmethod
    def delete(self, query):
        """Delete the first doc matching `query`. Return the number deleted."""

    @abstractmethod
    def count(self, query, extra=None):
        """Count docs matching `query` (with an optional native `extra` filter ANDed in)."""

    @abstractmethod
    def count_by(self, field):
        """Return a `{value: count}` map grouping docs by `field`."""

    @abstractmethod
    def nonconforming(self, schema):
        """Return the ids of docs that violate the given JSON Schema."""

    @abstractmethod
    def ensure_id_index(self):
        """Ensure the unique index on `id` exists, so a duplicate insert raises `dup_error`."""
