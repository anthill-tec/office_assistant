"""CR-OA-018 §S2 — embedded SQLite backend.

Stores one JSON document per row in a `<type>(id TEXT PRIMARY KEY, doc TEXT NOT NULL)`
table, compiling the neutral query/update model (`vidushi_oa.backends.query`) straight to
SQL over SQLite's JSON1 functions (`json_extract`/`json_each`). Write validation reuses the
packaged Mongo `$jsonSchema` validators, adapted to plain JSON Schema and checked with the
`jsonschema` package (the `[sqlite]` optional dependency).
"""
import json
import os
import sqlite3

import jsonschema

from vidushi_oa.backends import query as Q
from vidushi_oa.backends.base import Backend, Store

#: per-db-path cached connections (shared across backend instances for the same file)
_CONNS = {}
#: per-db-path registry of provisioned write-validation schemas (type -> schema)
_SCHEMAS = {}

#: neutral comparison ops that map to a plain SQL binary operator
_SQL_CMP = {"lt": "<", "lte": "<=", "gt": ">", "gte": ">="}

#: Mongo `bsonType` -> JSON Schema `type`
_BSON_MAP = {
    "string": "string", "object": "object", "array": "array", "bool": "boolean",
    "double": "number", "int": "number", "long": "number", "decimal": "number",
    "date": "string",
}


def _db_path():
    """Resolve the sqlite db file path from `VIDUSHI_SQLITE_PATH`, else the XDG data dir."""
    path = os.environ.get("VIDUSHI_SQLITE_PATH")
    if path:
        return path
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "vidushi-oa", "oa.db")


def _connect(path):
    """Return the per-path cached autocommit connection, creating the db dir on first use."""
    conn = _CONNS.get(path)
    if conn is None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.isolation_level = None  # autocommit: writes visible to other connections at once
        _CONNS[path] = conn
    return conn


def _map_bson(value):
    """Map a `bsonType` value (str or list) to the JSON Schema `type` value."""
    if isinstance(value, list):
        mapped = []
        for t in value:
            m = _BSON_MAP.get(t, t)
            if m not in mapped:
                mapped.append(m)
        return mapped[0] if len(mapped) == 1 else mapped
    return _BSON_MAP.get(value, value)


def _to_json_schema(node):
    """Recursively adapt a Mongo `$jsonSchema` dict to a plain JSON Schema dict."""
    if isinstance(node, dict):
        out = {}
        for key, val in node.items():
            if key == "bsonType":
                out["type"] = _map_bson(val)
            elif key == "properties":
                out[key] = {pk: _to_json_schema(pv) for pk, pv in val.items()}
            elif key == "items":
                out[key] = _to_json_schema(val)
            else:  # enum / pattern / required / additionalProperties pass through
                out[key] = val
        return out
    return node


def _validate(doc, schema):
    """Validate `doc` against the adapted schema; raises `jsonschema.ValidationError`."""
    jsonschema.validate(doc, _to_json_schema(schema))


def _leaf(expr, op, value):
    """Compile a single comparison on `expr` (a `json_extract(...)`) to `(sql, params)`."""
    if op == "eq":
        if value is None:  # null-or-missing parity with Mongo
            return f"{expr} IS NULL", []
        return f"{expr} = ?", [value]
    if op == "ne":  # missing counts as != (Mongo parity)
        return f"({expr} IS NULL OR {expr} != ?)", [value]
    if op == "in":
        placeholders = ",".join("?" for _ in value)
        return f"{expr} IN ({placeholders})", list(value)
    if op == "exists":
        return f"{expr} IS NOT NULL", []
    if op == "contains":
        term = str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return f"lower({expr}) LIKE '%'||lower(?)||'%' ESCAPE '\\'", [term]
    if op in _SQL_CMP:
        return f"{expr} {_SQL_CMP[op]} ?", [value]
    raise ValueError(f"unsupported op {op!r} for sqlite")


def _where(node):
    """Compile a neutral query node to a `(sql, params)` WHERE fragment over JSON1."""
    if isinstance(node, Q.Cond):
        return _leaf(f"json_extract(doc, '$.{node.path}')", node.op, node.value)
    if isinstance(node, Q.ElemMatch):
        parts, params = [], []
        for c in node.conds:
            frag, p = _leaf(f"json_extract(je.value, '$.{c.path}')", c.op, c.value)
            parts.append(frag)
            params.extend(p)
        inner = " AND ".join(parts) if parts else "1=1"
        return f"EXISTS (SELECT 1 FROM json_each(doc, '$.{node.path}') je WHERE {inner})", params
    if isinstance(node, Q.Group):
        subs = [_where(n) for n in node.nodes]
        frags = [s[0] for s in subs]
        params = [p for s in subs for p in s[1]]
        if node.kind == "all":
            return ("(" + " AND ".join(frags) + ")" if frags else "1=1"), params
        if node.kind == "any":
            return ("(" + " OR ".join(frags) + ")" if frags else "1=0"), params
        if node.kind == "none":
            return ("NOT (" + " OR ".join(frags) + ")" if frags else "1=1"), params
    raise TypeError(f"not a query node: {node!r}")


def _set_path(doc, path, value):
    """Set a dotted `path` in `doc`, creating intermediate dicts as needed."""
    parts = path.split(".")
    cursor = doc
    for part in parts[:-1]:
        cursor = cursor.setdefault(part, {})
    cursor[parts[-1]] = value


def _match_cond(elem, c):
    """Python-side evaluation of a `Cond` against an array element (for `resolve`)."""
    val = elem.get(c.path)
    if c.op == "eq":
        return val == c.value
    if c.op == "ne":
        return val != c.value
    if c.op == "in":
        return val in c.value
    if c.op == "exists":
        return (val is not None) == bool(c.value)
    raise ValueError(f"unsupported resolve op {c.op!r} for sqlite")


def _apply_update(doc, upd):
    """Apply a neutral `Update` to `doc` in place (set / push / resolve)."""
    for key, value in upd.set.items():
        _set_path(doc, key, value)
    for field, items in upd.push.items():
        doc.setdefault(field, []).extend(items)
    if upd.resolve:
        array_path, match_conds, set_fields = upd.resolve
        for elem in doc.get(array_path, []):
            if all(_match_cond(elem, c) for c in match_conds):
                elem.update(set_fields)
                break


class SqliteStore(Store):
    """Concrete `Store` over one sqlite table, compiling neutral nodes to SQL + JSON1."""

    def __init__(self, conn, type_, schema=None):
        self._conn = conn
        self._type = type_
        self._schema = schema
        self._ensured = False

    def _ensure(self):
        if not self._ensured:
            self._conn.execute(
                f"CREATE TABLE IF NOT EXISTS {self._type}(id TEXT PRIMARY KEY, doc TEXT NOT NULL)"
            )
            self._ensured = True

    @staticmethod
    def _project(doc, fields):
        if fields is None:
            return doc
        return {k: doc[k] for k in fields if k in doc}

    def find(self, query, fields=None, extra=None):
        if extra:
            raise NotImplementedError("--filter is mongo-only")
        self._ensure()
        where, params = _where(query)
        rows = self._conn.execute(f"SELECT doc FROM {self._type} WHERE {where}", params).fetchall()
        return [self._project(json.loads(r[0]), fields) for r in rows]

    def find_one(self, query, fields=None):
        self._ensure()
        where, params = _where(query)
        row = self._conn.execute(
            f"SELECT doc FROM {self._type} WHERE {where} LIMIT 1", params
        ).fetchone()
        return self._project(json.loads(row[0]), fields) if row else None

    def insert(self, doc):
        self._ensure()
        if self._schema is not None:
            _validate(doc, self._schema)
        self._conn.execute(
            f"INSERT INTO {self._type}(id, doc) VALUES(?, ?)", (doc["id"], json.dumps(doc))
        )

    def replace(self, id, doc):
        self._ensure()
        self._conn.execute(
            f"INSERT INTO {self._type}(id, doc) VALUES(?, ?) "
            f"ON CONFLICT(id) DO UPDATE SET doc=excluded.doc",
            (id, json.dumps(doc)),
        )

    def update(self, query, update, many=False):
        self._ensure()
        where, params = _where(query)
        limit = "" if many else " LIMIT 1"
        ids = [r[0] for r in self._conn.execute(
            f"SELECT id FROM {self._type} WHERE {where}{limit}", params).fetchall()]
        for id_ in ids:
            row = self._conn.execute(
                f"SELECT doc FROM {self._type} WHERE id=?", (id_,)).fetchone()
            doc = json.loads(row[0])
            _apply_update(doc, update)
            self._conn.execute(
                f"UPDATE {self._type} SET doc=? WHERE id=?", (json.dumps(doc), id_))
        return len(ids)

    def delete(self, query):
        self._ensure()
        where, params = _where(query)
        return self._conn.execute(f"DELETE FROM {self._type} WHERE {where}", params).rowcount

    def count(self, query, extra=None):
        if extra:
            raise NotImplementedError("--filter is mongo-only")
        self._ensure()
        where, params = _where(query)
        return self._conn.execute(
            f"SELECT COUNT(*) FROM {self._type} WHERE {where}", params).fetchone()[0]

    def count_by(self, field):
        self._ensure()
        rows = self._conn.execute(
            f"SELECT json_extract(doc, '$.{field}') AS v, COUNT(*) FROM {self._type} GROUP BY v"
        ).fetchall()
        return {str(v): n for v, n in rows}

    def nonconforming(self, schema):
        self._ensure()
        bad = []
        for (raw,) in self._conn.execute(f"SELECT doc FROM {self._type}").fetchall():
            doc = json.loads(raw)
            try:
                _validate(doc, schema)
            except jsonschema.ValidationError:
                bad.append(doc["id"])
        return bad

    def ensure_id_index(self):
        # `id` is already the PRIMARY KEY (unique); just make sure the table exists.
        # Idempotent — must NOT drop/recreate (no data loss).
        self._ensure()


class SqliteBackend(Backend):
    name = "sqlite"
    dup_error = sqlite3.IntegrityError

    def __init__(self):
        self._path = _db_path()

    def _conn(self):
        return _connect(self._path)

    def check(self):
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        return (True, f"SQLite at {self._path}")

    def collection(self, type_):
        raise NotImplementedError("sqlite backend has no raw collection handle; use store()")

    def store(self, type_):
        schema = _SCHEMAS.get(self._path, {}).get(type_)
        return SqliteStore(self._conn(), type_, schema)

    def db_name(self):
        return self._path

    def list_collections(self):
        rows = self._conn().execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        return [r[0] for r in rows]

    def provision(self, schemas):
        conn = self._conn()
        registry = _SCHEMAS.setdefault(self._path, {})
        for type_, schema in schemas.items():
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS {type_}(id TEXT PRIMARY KEY, doc TEXT NOT NULL)")
            registry[type_] = schema
        return list(schemas)
