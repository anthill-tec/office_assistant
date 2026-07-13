# DN — Persistence: JSONL → MongoDB, and the backend-owned state machine

> **Type:** DN (design note) · **Status:** ACCEPTED
> **Author:** Antony John · **Co-author:** Vidushi (orchestrator — office-assistant) · 2026-07-11
> **Informs:** CR-OA-001 … CR-OA-009 · **Design contract:** [`PRD-lifecycle-domain-model.md`](PRD-lifecycle-domain-model.md)

Captures the design rationale behind moving the store from flat JSONL + a stdlib CLI to MongoDB,
and the principle that shapes the whole framework. This reasoning lives here so the CRs stay pure
implementation contracts (no "why" narrative).

## Principle — the deterministic state machine lives in the backend

The framework is a **domain-specific state machine whose transition logic is locked into the
backend tools** — allowed transitions, computed states (EXPIRED from `expiry`, DUE from a renewal
window), the effects a transition fires (open/close actions, require a document), and schema
validation. The agent (LLM) spends tokens **only on judgment** it alone can do: scraping metadata
off messy mail/documents, deciding user-facing dispositions (KEEP/TOMBSTONE), drafting, and
choosing when to involve the user. Everything mechanical is a cheap, repeatable tool call.

| Backend tool — locked, token-free | Agent — reasoning, worth the tokens |
|---|---|
| valid transitions + their effects | reading messy mail, scraping metadata |
| computing DUE / EXPIRED from dates | classifying, deciding KEEP/TOMBSTONE |
| opening/closing the mapped actions | drafting messages, resolving ambiguity |
| schema validation on write | judging when to pull in the user |
| the `attention` worklist query | — |

## Decision — MongoDB

**Chosen: MongoDB (pymongo), one collection per store.** The decision variable was never
"JSON vs document DB" — JSONL is already a document model. It was **schema + validation + query
expressiveness + indexing headroom**, and Mongo delivers all four in one layer:

- **Schema representation + validation** — `$jsonSchema` collection validators encode the domain
  model (status enum, `actions[]` OPEN→RESOLVED shape, `documents[]`, catalogue
  `kind`/`relation`/`billing`, FK id patterns) and **reject malformed domain objects on write**.
- **JSON query language** — the strongest argument. Flat-file flag filters can't cleanly express
  "match into an `actions[]` array element" or "everything `DUE` in 30 days across insurance +
  registration + subscription". Mongo query documents (`{status:"DUE","actions.status":"OPEN"}`)
  and the aggregation pipeline do exactly that.
- **Documents in/out as JSON**; **indexing headroom** for if record counts ever grow.

### Alternative considered — JSONL + JSON Schema validation
A real contender: keep flat JSONL, add a stdlib JSON-Schema-subset validator, retain git/chezmoi
plain-text versioning and zero server. Rejected because it does **not** solve the query-
expressiveness need (nested-array + cross-domain queries would need hand-rolled Python), which is
where the growing domain model is headed. The schema work is not wasted — the same JSON Schemas
become the Mongo `$jsonSchema` validators.

### Indexing — deferred, not dismissed
At ~105 records every query is an instant scan; indexes beyond the unique `id` are not needed for
performance today. They're cheap to add (`status`, `product_id`, `actions.status`, `expiry`) and
we do, purely as headroom — not because scale demands it.

## Consequences (accepted)

- **New dependencies** — a running `mongod` + `pymongo`. This **reverses the project's former
  "stdlib-only, no server" cardinal rule**; `CLAUDE.md` is updated to match (CR-OA-008).
- **Mongo becomes the primary store; git holds readable snapshots.** Versioning is a single
  explicit **`store.py snapshot`** feature (pure pymongo — `mongoexport`/`mongodump` are not
  installed) that exports each collection → `data/*.jsonl` on demand, so the chezmoi/git plain-
  text history + backup continue. Run it before a commit checkpoint — same cadence as a commit.
- **CLI compatibility is preserved** — `store.py`'s verbs/flags keep working so the caller skills
  (invoice-tracker, warranty-tracker, support-case-manager, product-catalogue) don't change; the
  MongoDB `_id` is suppressed from all output and the string `id` stays the app-level key.
- **No lock-in** — the `$jsonSchema` validators are the same contract a future backend would use;
  the JSONL snapshots remain importable, giving an instant rollback path.

## Fixed choices

- Instance **port 27017**, database **`office_assistant`** (local, no auth). Port 27018 hosts the
  user's CodeForge/Velocity platform DBs and is **off-limits**.
- Versioning = one explicit `snapshot` verb (no per-write hook).
- Connection via env `OA_MONGO_URI` (default `mongodb://127.0.0.1:27017`) + `OA_MONGO_DB`
  (default `office_assistant`); no secrets in code.
