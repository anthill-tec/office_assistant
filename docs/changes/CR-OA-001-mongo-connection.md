# CR-OA-001 — MongoDB connection & collection bootstrap

**Status:** PENDING
**Type:** feature
**Priority:** High
**Depends on:** —
**Labels:** mongo, infra
**Phase:** Wave 1
**Design reference:** docs/research/DN-persistence-mongodb.md (Fixed choices); PRD §8
**Author:** Antony John · **Co-author:** Claude (orchestrator — office-assistant)

## Context

The store moves from flat JSONL to MongoDB (see DN). This CR lays the connection + collection
foundation only — no change yet to how the caller skills use `store.py` (that is CR-OA-003).

## Scope

### §S1 Connection helper — `scripts/oa_mongo.py`
A stdlib+pymongo module exposing `client()`, `db()`, `coll(type)`. Reads env `OA_MONGO_URI`
(default `mongodb://127.0.0.1:27017`) and `OA_MONGO_DB` (default `office_assistant`); short
`serverSelectionTimeoutMS`. No secrets in code. The URI default is pinned to 27017 (27018 hosts
the user's platform DBs and is off-limits).

### §S2 Collection registry
Extend `STORES` in `store.py` to a `type → collection-name` map:
`contacts, invoices, warranties, cases, products` (same names). `oa_mongo.coll(t)` returns the
`Collection` for type `t`.

### §S3 `store.py init`
A verb that creates the 5 collections if absent and ensures a **unique index on `id`** for each.
Idempotent (safe to re-run).

## Acceptance criteria
- [ ] §S1 `oa_mongo.db().name == "office_assistant"` and `oa_mongo.client().address == ("127.0.0.1", 27017)` with no env override; setting `OA_MONGO_DB=foo` makes `db().name == "foo"`.
- [ ] §S2 `store.STORES` keys are exactly `{contacts, invoices, warranties, cases, products}`; `oa_mongo.coll("invoices").name == "invoices"` and `.database.name == "office_assistant"`.
- [ ] §S3 After `python3 scripts/store.py init`, for each store `coll(t).index_information()` contains an index over key `id` with `unique == True`; a second `init` exits 0 and adds nothing.
- [ ] §S3 (caller) `init` is a real subparser (`store.py init` in `--help`); it is the entry point CR-OA-006 `import` depends on.

## Estimated size
S — one new module + ~30 lines in `store.py`.

## Risk
Two `mongod` instances are running (27017, 27018). The URI default MUST resolve to 27017; a test
asserts the client address to prevent ever writing to 27018.

## Non-goals
No CRUD rewrite (CR-OA-003), no schema validators (CR-OA-002), no data import (CR-OA-006).
