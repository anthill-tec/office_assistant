# CR queue — office-assistant (OA)

Single source of truth for Change-Request **process state**: status, dependencies, wave, and
ordering. Pick the next `PENDING` CR whose dependencies are all `COMPLETED`. Status lives here —
never inside a CR spec file.

- **Design contract:** [`../research/PRD-lifecycle-domain-model.md`](../research/PRD-lifecycle-domain-model.md)
- **Decision note:** [`../research/DN-persistence-mongodb.md`](../research/DN-persistence-mongodb.md)
- **Canonical states:** `PENDING` · `IN_PROGRESS` · `COMPLETED` · `SUPERSEDED` · `DEFERRED`.

## Execution model — Solo single-orchestrator (no Mainline / parallel Tracks)

One orchestrator executes the queue **sequentially** by dependency/wave order — no Model-B
Mainline + parallel Track workers, no Sandesh coordination. Two-phase per the house convention:

- **Design phase → `main`** (integration branch): this queue, the PRD, the DN, and the CR specs
  themselves commit to `main`.
- **Execution phase → a per-CR feature branch**: each CR gets its own branch
  (`feature/CR-OA-NNN-<slug>`), RED→GREEN→VERIFY against its ACs, then merge to `main`. Only one
  CR is `IN_PROGRESS` at a time.

## Queue

| CR | Title | Type | Status | Depends on | Wave |
|---|---|---|---|---|---|
| [CR-OA-001](CR-OA-001-mongo-connection.md) | Mongo connection & collection bootstrap | feature | COMPLETED (2026-07-11) | — | 1 |
| [CR-OA-002](CR-OA-002-schema-validators.md) | Domain JSON-Schema validators + `validate` | feature | COMPLETED (2026-07-11) | 001 | 1 |
| [CR-OA-003](CR-OA-003-store-crud-pymongo.md) | `store.py` CRUD on pymongo (CLI-compatible) | feature | COMPLETED (2026-07-11) | 001 | 2 |
| [CR-OA-004](CR-OA-004-tracking-verbs-pymongo.md) | Tracking verbs on pymongo | feature | COMPLETED (2026-07-11) | 003 | 2 |
| [CR-OA-006](CR-OA-006-migration-and-snapshot.md) | Migration `import` + `snapshot` versioning | feature | COMPLETED (2026-07-11) | 003 | 2 |
| [CR-OA-005](CR-OA-005-state-machine-engine.md) | Transition-map state-machine engine + `event` | feature | COMPLETED (2026-07-12) | 003, 004 | 3 |
| [CR-OA-007](CR-OA-007-subscriptions-insurance-stores.md) | `subscriptions` + `insurance` stores + memory migration | feature | COMPLETED (2026-07-12) | 002, 005, 006 | 3 |
| [CR-OA-008](CR-OA-008-docs-and-rules.md) | Docs & rules refresh | docs | COMPLETED (2026-07-12) | 001–007 | 4 |
| [CR-OA-009](CR-OA-009-toon-output.md) | TOON output for the store CLI (AXI interface) | feature | PENDING | 003, 004, 005, 007 | 4 |

**Recommended order:** 001 → 002 → 003 → **006 → 004** → 005 → 007 → 009 → 008.
(2026-07-11: 006 pulled ahead of 004 — after the CRUD refactor the Mongo store is empty and the
tracking verbs still read JSONL; importing next repopulates Mongo so the store is functional
end-to-end. Both 006 and 004 depend only on the now-shipped 003.)

### Notes
- **CR-OA-009 pivoted (2026-07-12):** the MCP-server scope was **dropped** for **TOON output over the
  CLI** (the AXI stance) — lower per-task tokens, one pinned dependency instead of ~28, nothing to
  enable/reload. Rationale + the library-verification finding are in
  [`../research/DN-agent-interface-toon.md`](../research/DN-agent-interface-toon.md); the spec was renamed
  `CR-OA-009-mcp-interface.md → CR-OA-009-toon-output.md` and now depends on 007 (its ACs read the
  `subscriptions` store).
- The already-built `store.py` v1 tracking verbs + the applied JSONL backfill (48 invoices
  COMPLETED, 19 warranties IN_PROGRESS, FNIRSI actions OPEN) are the **starting point** CR-OA-003
  / 004 port onto pymongo — not to be redone.
- `data/*.jsonl` stay as the `snapshot` target (chezmoi-versioned); they are NOT committed to the
  project repo (gitignored). Mongo data lives on the local instance only.

## Follow-up tasks
Small items (no design surface → tasks, not CRs) surfaced during execution:
- **Coverage source path** (filed 2026-07-11, from CR-OA-002 regression gate) — `python-crucible
  regression --coverage` runs `coverage run --source app`, but this project's code is in `scripts/`,
  so no coverage is collected (`No data was collected`). Point coverage at `scripts/` (a `.coveragerc`
  `[run]\nsource = scripts`, or a `--source` override in the gate). Tests still gate green; only the
  coverage metric is missing.
- **`data/schema.md` `cases.status` enum** (filed 2026-07-11, from CR-OA-002) — schema.md still shows
  the old lowercase `open|awaiting_support|…|closed`; the store now enforces the shared 6-value
  uppercase lifecycle `status`. **Resolved in CR-OA-008 §S2 (2026-07-12)** — schema.md `cases.status`
  now documents the 6-value uppercase lifecycle; `grep -c awaiting_support data/schema.md == 0`.
- **`store.py:70` PEP8 spacing** (filed 2026-07-11, from CR-OA-003 VERIFY) — missing blank-line pair
  before `def path(t):` after Cycle A removed `_CACHE`; cosmetic, fold into a lint pass.
- **Disposition-aware `due-sweep` action** (filed 2026-07-12, from CR-OA-007 live dry-run) — the
  `renewal-window` transition opens `cancel-before-charge` uniformly for every subscription, but a
  **KEEP** sub (Fastmail, Anthropic) reaching its renewal window wants a `renewal-confirm`/protect
  action, not a cancel prompt; only **TOMBSTONE/UNDECIDED** subs should get `cancel-before-charge`.
  Make the opened action disposition-aware (transition effect keyed on `disposition`, or a
  post-sweep pass). Until then, do NOT run a live `due-sweep` on the migrated KEEP subscriptions.
