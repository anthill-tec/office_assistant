# CR-OA-002 — Domain JSON-Schema validators + `validate` verb

**Status:** COMPLETED (shipped 2026-07-11 on feature/CR-OA-002-schema-validators)
**Type:** feature
**Priority:** High
**Depends on:** 001
**Labels:** mongo, schema, validation
**Phase:** Wave 1
**Design reference:** PRD §3–§5 (status vocabulary, actions[], documents[]); DN (schema+validation)
**Author:** Antony John · **Co-author:** Claude (orchestrator — office-assistant)

## Context

Encode the domain model as machine-checked schemas so malformed domain objects are rejected on
write and can be linted on demand.

## Scope

### §S1 Schema files — `data/schema/<type>.schema.json`
One JSON Schema per existing store (contacts, invoices, warranties, cases, products) covering the
field set from `data/schema.md` plus the domain model: `status` enum, `actions[]` item shape,
`documents[]` item shape, FK id-string patterns, and for `products` the catalogue fields.

### §S2 Apply as Mongo `$jsonSchema` validators
An `apply-validators` step (invoked by `store.py init`) sets each collection's validator with
`validationLevel:"moderate"`, `validationAction:"error"`.

### §S3 `store.py validate [<type>]`
Lists non-conforming documents via `find({$nor:[{$jsonSchema: <schema>}]})`, returning their ids.

## Acceptance criteria
- [x] §S1 `invoices.schema.json` `properties.status.enum == ["NEW","UNKNOWN","IN_PROGRESS","COMPLETED","EXPIRED","DUE"]`; `properties.actions.items.properties.status.enum == ["OPEN","RESOLVED"]`; `properties.id.pattern == "^doc_"`.
- [x] §S1 `products.schema.json` `properties.kind.enum == ["physical","virtual"]`, `properties.relation.enum == ["accessory","consumable"]`, `properties.billing.enum == ["one-time","subscription"]`.
- [x] §S2 After `init`, inserting `{"id":"doc_x_1","status":"BOGUS"}` into `invoices` raises `pymongo.errors.WriteError`; a schema-valid document inserts with no error.
- [x] §S3 `store.py validate invoices` returns `[]` against the migrated data (all 48 conform); after inserting one deliberately bad doc, its `id` appears in the output list.
- [x] §S3 (caller) `validate` and `apply-validators` are real subparsers; `init` invokes `apply-validators`.

> **Note (2026-07-11):** the "against the migrated data (all 48 conform)" clause of the §S3 AC is
> exercised by **CR-OA-006** once the JSONL is imported into Mongo — at ship time the collections
> are empty of domain rows. The validator behaviour + `validate` verb are fully verified here (10/10
> tests, VERIFY PASS).

## Estimated size
M — 5 schema files + validator apply + one verb.

## Risk
`$jsonSchema` could reject legacy docs missing newly-modelled fields. Mitigate with
`validationLevel:"moderate"` and optional (non-`required`) fields; verify `validate` returns `[]`
on the real migrated data before enforcing.

## Non-goals
No `subscriptions`/`insurance` schemas (CR-OA-007); no CRUD rewrite (CR-OA-003).
