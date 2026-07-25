"""CR-OA-018 — neutral, backend-agnostic query/update model.

The CLI builds these; each backend compiles them to its OWN native query (Mongo -> a query
document; SQLite -> SQL + JSON1). No backend's dialect is the interface, and there is no
dialect-translation layer.

A query node is a `Cond` (scalar comparison on a dotted path), an `ElemMatch` (an array field
where at least one element matches a set of `Cond`s), or a `Group` (`all`/`any`/`none` of nodes).
`ALL` (an empty `all`-group) matches everything. Updates are a `set` map, a `push` map
(field -> items appended), and/or a `resolve` (update the first element of an array matching some
`Cond`s).
"""
from dataclasses import dataclass, field
from typing import Any, Dict, Tuple

#: scalar comparison operators a `Cond` may use
OPS = frozenset({"eq", "ne", "in", "lt", "lte", "gt", "gte", "exists"})


@dataclass(frozen=True)
class Cond:
    path: str            # dotted path, e.g. "status" or "source.email_id"
    op: str              # one of OPS
    value: Any = None


@dataclass(frozen=True)
class ElemMatch:
    path: str            # array field, e.g. "actions"
    conds: Tuple         # tuple[Cond] every matched element must satisfy (AND)


@dataclass(frozen=True)
class Group:
    kind: str            # "all" | "any" | "none"
    nodes: Tuple         # tuple of nodes (Cond | ElemMatch | Group)


@dataclass(frozen=True)
class Update:
    set: Dict = field(default_factory=dict)
    push: Dict = field(default_factory=dict)          # field -> list of items appended
    resolve: Any = None                               # (array_path, match_conds: tuple[Cond], set_fields: dict)


# ── builders ──────────────────────────────────────────────────────────────────
def cond(path, op, value=None):
    if op not in OPS:
        raise ValueError(f"unknown op {op!r}; known: {sorted(OPS)}")
    return Cond(path, op, value)


def elem(path, *conds):
    return ElemMatch(path, tuple(conds))


def all_(*nodes):
    return Group("all", tuple(nodes))


def any_(*nodes):
    return Group("any", tuple(nodes))


def none_(*nodes):
    return Group("none", tuple(nodes))


#: matches every record
ALL = Group("all", ())
