# CR-OA-006 — Migration `import` + `snapshot` versioning

**Status:** PENDING
**Type:** feature
**Priority:** High
**Depends on:** 003
**Labels:** mongo, migration, versioning
**Phase:** Wave 2
**Design reference:** DN (snapshot versioning; mongoexport not installed → pure pymongo)
**Author:** Antony John · **Co-author:** Claude (orchestrator — office-assistant)

## Context

Move the current 105 JSONL records into Mongo and establish the explicit snapshot export that
keeps the chezmoi/git plain-text versioning. `mongoexport`/`mongodump` are not installed — both
directions are pure pymongo.

## Scope

### §S1 `store.py import [<type>]`
Read `data/<file>.jsonl` and upsert each record by `id` (`replace_one({id}, doc, upsert=True)`).
Idempotent — re-running changes nothing.

### §S2 `store.py snapshot [<type>]`
Export each collection → `data/<file>.jsonl`: strip `_id`, emit keys in a **stable order** (e.g.
`id` first then sorted), one JSON object per line, newline-terminated. This is the single
versioning feature — run before a chezmoi/git checkpoint.

### §S3 Round-trip integrity
`import` → `snapshot` → `import` yields an identical document set.

## Acceptance criteria
- [ ] §S1 After `store.py import`, `store.py stats <t>` `total` equals `wc -l data/<file>.jsonl` for each: invoices 48, contacts 18, warranties 19, cases 1, products 19 (105 total).
- [ ] §S1 Re-running `store.py import` leaves every collection count unchanged and creates no duplicate `id`.
- [ ] §S2 After `store.py snapshot`, `git diff --stat data/` shows only key-order normalization — no record added or dropped; every emitted line is valid JSON and contains no `_id` key.
- [ ] §S3 The sorted `(id, updated)` checksum of each collection is identical before and after an `import → snapshot → import` cycle.
- [ ] **Caller:** `import` and `snapshot` are real subparsers; `snapshot` is the versioning entry point named in the DN.

## Estimated size
M — two verbs + a stable serialiser (reuse `store.py`'s JSON dump path, keys ordered).

## Risk
Key-order churn producing noisy `git diff`s — normalise key order on export (id-first, then
sorted) so snapshots are stable across runs.

## Non-goals
No new stores (CR-OA-007); no schema changes (CR-OA-002).
