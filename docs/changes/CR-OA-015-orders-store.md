# CR-OA-015 — `orders` delivery-lifecycle store

**Status:** PENDING
**Type:** feature
**Priority:** High
**Depends on:** 005, 007
**Labels:** store, domain, orders, fulfilment
**Phase:** Wave 7 (unified-skill parity)
**Design reference:** [DN-purchases-persistence.md](../research/DN-purchases-persistence.md) · PRD-lifecycle-domain-model §3, §6
**Author:** Antony John · **Co-author:** Vidushi (orchestrator — office-assistant)

## Context

The purchase/delivery domain has no store — the legacy `purchase-tracker` kept a memory note, and
the unified skill points at a non-existent "order tracking" store. Per DN-purchases-persistence
(Decision A), add a dedicated **`orders`** store: the fulfilment state machine, disentangled from
`invoices` (which stays the pure proof-of-purchase document). This is the backend the unified
skill's purchase domain (CR-OA-016) wires to.

## Scope

### §S1 Register the `orders` store
**Surfaces (verify at gap-analysis):** `STORES`, `PREFIX`, `FK_MAP` in `vidushi_oa/_cli.py`;
`vidushi_oa/schema/*.schema.json`.
- Add `orders` to `STORES` (snapshot file `orders.jsonl`) and to `PREFIX` with anchor
  `ord_<merchant>_<number|date>`.
- Add `vidushi_oa/schema/orders.schema.json`: `status ∈ {NEW,UNKNOWN,IN_PROGRESS,COMPLETED}`,
  `actions[].status ∈ {OPEN,RESOLVED}`, order fields per the DN; `invoice_id` / `product_id`
  optional strings.
- Extend `FK_MAP`: `invoice_id`→`invoices`, `product_id`→`products`.
- `voa init` creates the `orders` collection, its unique `id` index, and applies the `$jsonSchema`
  validator.

### §S2 `orders` transition map
`transitions.py` `TRANSITIONS["orders"]` maps events → transition + effects: `shipped` /
`out-for-delivery` advance `stage` within `IN_PROGRESS`; `delivered` → `COMPLETED`;
`held-at-customs` / `duty-demanded` / `kyc-requested` / `clarification-requested` open the matching
OPEN action; `cancelled` / `returned` / `refunded` / `delivery-failed` set the terminal side-state.
An unmapped event is rejected with **no write** (per the CR-005 engine).

### §S3 `delivery-sweep` verb
`voa delivery-sweep [--dry-run]`: for each order in `{NEW,UNKNOWN,IN_PROGRESS}`, if
`last_event_date` is > 7 days ago open a `stuck-chase` action (idempotent — skip if one is already
OPEN); if `eta` < today and status ≠ `COMPLETED`, flag it. Emits the TOON envelope with the count of
rows touched. Rows land in `voa attention`.

### §S4 schema.md + PRD §3 reconciliation
- Document the `orders` store in `data/schema.md` (fields, shared status, action vocabulary, FKs).
- Revise **PRD §3**: move the **Purchase / fulfilment** row from `invoices` to `orders`; restate the
  `invoices` row as the proof-of-purchase **document** domain. (Commits on this CR's feature branch.)

## Acceptance criteria

### §S1
- [ ] `voa add orders --json '{"merchant":"Acme","number":"A1",...}'` mints id `ord_acme_a1`; `STORES` and `PREFIX` both contain `orders`.
- [ ] `orders.schema.json` **rejects** `{"status":"delivered"}` (not in `{NEW,UNKNOWN,IN_PROGRESS,COMPLETED}`) with a `WriteError`, and **accepts** `{"status":"IN_PROGRESS"}`.
- [ ] `voa get orders <id> --expand invoice_id,product_id` returns `invoice_id_obj` / `product_id_obj` when the FKs are set (FK_MAP wired).
- [ ] After `voa init`, the `orders` collection has a `unique:true` `id_1` index and a `$jsonSchema` validator; `voa validate orders` on seeded valid rows returns `[0]:`.

### §S2
- [ ] `voa event orders <id> delivered` on an `IN_PROGRESS` order → status `COMPLETED`; an unmapped event (e.g. `bogus`) errors with **no** write to the row.
- [ ] `voa event orders <id> held-at-customs` opens an OPEN `customs-clearance` action; `voa attention orders` then lists that row.

### §S3
- [ ] An `IN_PROGRESS` order with `last_event_date` 8 days ago → `voa delivery-sweep` opens exactly **one** `stuck-chase` action; a second `delivery-sweep` opens **none** (idempotent); `--dry-run` writes nothing.
- [ ] Caller-existence: `voa --help` lists `delivery-sweep`, and a non-test path invokes the sweep function (grep returns ≥1 non-test caller).

### §S4
- [ ] `data/schema.md` documents the `orders` status + action vocabulary and its `invoice_id` / `product_id` FKs.
- [ ] PRD §3 "Purchase / fulfilment" row names store `orders`; the `invoices` row is restated as the document domain.

## Estimated size
M — one store on the existing domain-agnostic rails (STORES / PREFIX / FK_MAP / validator /
transitions) + one sweep verb; no data migration.

## Risk
A buyer may expect the `invoices` row to *be* the order — mitigated by the DN's document-vs-lifecycle
split and the `invoice_id` link. The customs-without-order path (a minimal row from a bare AWB) is
authored in the skill (CR-OA-016), not baked into this store CR.

## Non-goals
Back-filling historical orders from mail (populated going forward); an ICEGATE/carrier integration;
altering existing `invoices` data.
