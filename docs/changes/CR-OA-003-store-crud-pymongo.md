# CR-OA-003 — `store.py` CRUD on pymongo (CLI-compatible)

**Status:** COMPLETED (shipped 2026-07-11 on feature/CR-OA-003-store-crud-pymongo)
**Type:** feature
**Priority:** High
**Depends on:** 001
**Labels:** mongo, store, compatibility
**Phase:** Wave 2
**Design reference:** DN (CLI compatibility preserved; `_id` suppressed); PRD §2–§6
**Author:** Antony John · **Co-author:** Vidushi (orchestrator — office-assistant)

## Context

Port the read/write CRUD off JSONL onto pymongo while preserving the **exact** CLI contract the
four caller skills use (invoice-tracker, warranty-tracker, support-case-manager, product-catalogue),
so they need no change. The stringified `--where` equality and substring `--contains` are the
migration's biggest semantic landmines.

## Scope

### §S1 Backend swap + `_id` suppression
Replace `load`/`save` with pymongo collection ops. Apply a global `{_id: 0}` projection so a Mongo
`ObjectId` never appears in output (it is not JSON-serialisable and would change the record key set
callers read back). The string `id` stays the app-level key; `gen_id`, dedupe and every FK use it.

### §S2 `query` filter translation (preserve today's semantics)
- `--where f=v`: **type-coerce** `v` — `None`/`null` → `{f:{$in:[None]}}` (null-or-missing),
  ints/floats numeric, `true`/`false` boolean, else string. Dotted paths are native Mongo keys.
- `--contains f=sub`: `{f:{$regex: re.escape(sub), $options:"i"}}` (matches string and
  array-of-string fields).
- `--after/--before f=D`: `{f:{$gte:D}}` / `{f:{$lte:D}}` on ISO strings (inclusive).
- `--fields` dotted projection (with `_id:0`); `--sort` → `.sort`; `--limit` → `.limit`;
  `--expand fk` → second `find` by `id` in `FK_MAP[fk]` → `<fk>_obj`.
- New `--filter '{json}'`: a native Mongo query document, ANDed with the flag-derived filter.

### §S3 `get` / `add` / `update` / `rm` / `stats`
- `add`: `gen_id` + dedupe via the unique `id` index (`DuplicateKeyError` → `skipped`); response
  `{"added":[...],"skipped":[...]}` unchanged. Bulk array still accepted.
- `update`: shallow-merge → `$set` of the patch's top-level keys; `--append-log` → `$push log`;
  bumps `updated`. Response `{"updated": id}` unchanged.
- `rm` → `{"removed":id,"remaining":N}`; `stats --by f` via `$group`.

## Acceptance criteria
*(Capture a pre-migration baseline of each command's output on the JSONL store first; assert equality.)*
- [x] §S2 `query invoices --where file=None --fields id` returns the same id set as the JSONL baseline.
- [x] §S2 `query invoices --where vendor=FNIRSI --contains number=75752 --fields id` returns the baseline id(s); `query warranties --fields product,expiry --sort expiry` returns rows in the baseline order.
- [x] §S1 No `_id`/`ObjectId` substring appears in the stdout of `query`, `get`, or `get … --expand` for any store.
- [x] §S1 `get products prod_maruti-suzuki_ritz-lxi --expand invoice_id,warranty_id,contact_id` embeds `<fk>_obj`, and neither the record nor any `_obj` contains `_id`.
- [x] §S3 Re-`add invoices --json '{"id":"<existing id>", ...}'` returns `{"added":[],"skipped":["<existing id>"]}`; a new id returns `{"added":["<new>"],"skipped":[]}`.
- [x] §S3 `update invoices <id> --json '{"file":"x.pdf"}'` sets `file` and updates `updated`; `stats invoices --by status` counts equal a client-side count of the collection.
- [x] §S2 `--filter '{"status":"COMPLETED"}'` returns exactly the COMPLETED invoices.
- [x] **Integration/caller:** the four skills' documented `store.py` invocations run unchanged and return baseline-equal output; `grep -rn "store.py" ~/.claude/skills` confirms ≥1 non-test caller per verb used.

> **Note (2026-07-11):** the "same id set **as the JSONL baseline**" clauses are verified here on
> **seeded fixtures** (CLI semantics: null/numeric coercion, array `--contains`, `_id` suppression,
> `--expand`, dedupe, `$set`/`$push`, stats). Parity against the real 105-record data is a **CR-OA-006**
> check (Mongo is empty until the import). The tracking verbs still read/write JSONL until **CR-OA-004**.

## Estimated size
L — the central refactor; ~all of `store.py`'s command bodies.

## Risk
`--where` type-coercion (`file=None`, `amount=0`) and `--contains` on array fields are the top
landmines — a subtle change silently breaks skill de-dupe (→ duplicate `add`s). De-risk by
diffing every representative command against the captured JSONL baseline before merge.

## Non-goals
Tracking verbs (CR-OA-004), the `event` state machine (CR-OA-005), data import (CR-OA-006).
