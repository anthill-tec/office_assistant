# CR-OA-008 — Docs & rules refresh

**Status:** COMPLETED (shipped 2026-07-12 on feature/CR-OA-008-docs-and-rules)
**Type:** docs
**Priority:** Medium
**Depends on:** 001, 002, 003, 004, 005, 006, 007
**Labels:** docs
**Phase:** Wave 4
**Design reference:** DN (consequences: Mongo primary, pymongo dep, snapshot versioning)
**Author:** Antony John · **Co-author:** Claude (orchestrator — office-assistant)

## Context

Bring the project docs in line with the migrated system so no doc still describes the retired
stdlib-only JSONL model.

## Scope

### §S1 `CLAUDE.md`
Replace the cardinal rule / "the only executable is the JSONL data CLI" framing with: Mongo is
primary (port 27017, db `office_assistant`), `pymongo` is a dependency, `data/*.jsonl` are
`snapshot` outputs for chezmoi. Document the new verbs (`init`, `validate`, `event`, `import`,
`snapshot`) and the connection env vars in the commands section.

### §S2 `data/schema.md`
Document the tracking-state model: `status` enum, `actions[]` shape, `documents[]`, the transition
model, the `products` catalogue fields (`kind`/`relation`/`billing`), and the new `subscriptions`
and `insurance` stores. **Reconcile the `cases.status` enum** — since CR-OA-002 the store enforces
the shared 6-value uppercase lifecycle `status` on `cases`; replace schema.md's old lowercase
`open|awaiting_support|awaiting_user|rma_issued|in_repair|resolved|closed` (whose per-stage detail
now lives in `actions[]`).

### §S3 Other docs
Update `scripts/README.md`, `README.md`, `reference/README.md`, `documents/README.md` — store CLI
references + the Mongo/snapshot note.

## Acceptance criteria
- [x] §S1 `grep -c "only executable is the JSONL" CLAUDE.md == 0`; `CLAUDE.md` contains `store.py init`, `validate`, `event`, `import`, `snapshot` and `OA_MONGO_URI`/`OA_MONGO_DB`.
- [x] §S2 `data/schema.md` documents the `status` enum (all 6 values), the `actions[]` item shape (`OPEN`/`RESOLVED`), `documents[]`, and a section each for the `subscriptions` and `insurance` stores.
- [x] §S2 `data/schema.md` `cases.status` documents the shared 6-value `status` (no lingering `open|awaiting_support|…|closed` lowercase enum); `grep -c "awaiting_support" data/schema.md == 0`.
- [x] §S3 `grep -rl "stdlib" README.md scripts/README.md` shows no doc still claiming the store is stdlib-only JSONL; each README's store CLI example runs successfully against Mongo.

## Estimated size
M — doc edits across 6 files.

## Risk
Doc drift — grep-based ACs catch stale claims.

## Non-goals
No code changes (this is a docs CR); MCP docs land with CR-OA-009.
