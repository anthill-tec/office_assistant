# Purchase / fulfilment persistence — a dedicated `orders` store

> **Type:** DN (design note) · **Status:** ACCEPTED (2026-07-13)
> **Author:** Antony John · **Co-author:** Vidushi (orchestrator — office-assistant)
> Informs: CR-OA-015 (the store) · CR-OA-016 (the unified skill's purchase domain).
> Design context: [PRD-lifecycle-domain-model.md](PRD-lifecycle-domain-model.md) §3, §6.

## The gap

The purchase/delivery domain is the one interactive domain with **no realized store**. The
legacy `purchase-tracker` skill persisted its running picture to a `purchases-tracker.md` memory
note; the unified `vidushi-oa` skill (CR-OA-012) points its purchase domain at *"store type via
order tracking"* — a placeholder for a collection that does not exist. Its cross-run value —
spotting a **STUCK** order (no movement since the last scan) or an order whose **ETA has passed**
without a delivered event — needs a durable row, so this must resolve to a real store.

PRD §3 currently assigns **Purchase / fulfilment** to the **`invoices`** store (status
`NEW→…→COMPLETED` = item delivered; shipment/customs/duty as its actions). That **overloads
`invoices`** to be *both* the proof-of-purchase document *and* the fulfilment state machine — and
an order confirmation exists **before** any invoice/receipt, so a fulfilment lifecycle cannot
reliably hang off a row that may not exist yet.

## Options considered

- **A — dedicated `orders` store (CHOSEN).** Fulfilment is its own domain object (its own state
  machine + action set), FK-linked to its `invoices` proof and its `products` asset. Faithful to
  the legacy purchase-tracker; cross-run STUCK/ETA detection works; removes the `invoices`
  overload. Cost: one new store + a `delivery-sweep` (a feature/code CR).
- **B — extend `invoices` with a `delivery{}` block.** No new collection; a nested
  `{stage, carrier, awb, eta, last_event}` on the invoice row. Cheaper, but keeps the
  document/lifecycle overload and leaves **pre-invoice orders untracked** (no invoice row yet).
- **C — document-first, no order store.** Purchases reconstructed live from mail each run; only
  the invoice persists. Simplest and zero code, but **loses cross-run STUCK/ETA-passed detection**
  — i.e. it does *not* do the same job as `purchase-tracker`.

**Decision (2026-07-13): A.** Rationale: each domain object is its own state machine (PRD §1); an
order precedes its invoice; and the running-picture value the skill exists to provide requires a
durable, sweep-able row. A also *cleans up* the PRD by disentangling the proof-of-purchase
**document** (`invoices`) from the fulfilment **lifecycle** (`orders`).

## The `orders` domain (design)

- **Store:** `orders`. **Id:** `ord_<merchant>_<number|date>`.
- **Status** (shared vocab): `NEW → UNKNOWN → IN_PROGRESS → COMPLETED`. `COMPLETED` = delivered
  (or a terminal side-state). Not a recurring domain — no `DUE`.
- **Stage** (human-readable, informational — distinct from the coarse `status` that drives
  tracking): `Ordered · Paid · Processing · Shipped · In transit · Customs clearance · Out for
  delivery · Delivered`, plus terminal side-states `Cancelled · Returned · Refunded ·
  Delivery-failed · RTO`.
- **Action set (OPEN→RESOLVED):** `payment · shipment · out-for-delivery · delivery ·
  customs-clearance · duty-payment · kyc · clarification · redelivery · return · refund ·
  stuck-chase`. **Customs sub-states are OPEN actions** (`customs-clearance` / `duty-payment` /
  `kyc` / `clarification`), so `voa attention` surfaces a parcel awaiting the user.
- **Fields:** `merchant`, `number`, `items[]`, `amount`, `currency`, `order_date`, `carrier`,
  `tracking` (AWB), `eta`, `stage`, `last_event`, `last_event_date`, `alias`, `acct`,
  `invoice_id` (FK→`invoices`), `product_id` (FK→`products`), `source`, plus the shared
  `status` / `actions[]` / `documents[]`.
- **FK_MAP additions:** `invoice_id`→`invoices`, `product_id`→`products` (an order → its proof +
  the asset it delivers).
- **`delivery-sweep`** (agent-owned, sibling of `warranty-sweep` / `due-sweep`): for each order in
  `{NEW, UNKNOWN, IN_PROGRESS}`, if `last_event_date` is older than **7 days** → open a
  `stuck-chase` action (idempotent — not re-opened while one is already OPEN); if `eta` is past and
  status ≠ `COMPLETED` → flag. `--dry-run` supported.
- **Customs without a matching order:** a standalone customs / broker / India-Post (FPO) mail that
  carries only an **AWB** may create a minimal `orders` row (or annotate the one matched by AWB),
  `status: IN_PROGRESS` + an OPEN customs action — so a duty/KYC demand is never missed even when
  it arrives outside the order thread.

## PRD reconciliation (lands with CR-OA-015)

CR-OA-015 revises **PRD §3**: the **Purchase / fulfilment** row moves from `invoices` to the new
**`orders`** store, and the `invoices` row is restated as the **proof-of-purchase DOCUMENT** domain
(doc_type / number / amount / GST + the pinned `source`). The two link via `invoice_id`. This
removes the store overload. (Per the doc conventions, that PRD edit commits on CR-OA-015's feature
branch, since it makes no sense without the store it describes.)

## Consequences

- **+1 store (8th collection).** `voa init`, the sweeps, `attention`, `validate`, and TOON output
  all apply for free — the CLI is domain-agnostic once a store is registered (PRD §8).
- The unified skill's purchase domain (CR-OA-016 §S2) wires to `orders` instead of the phantom
  placeholder; `delivery-sweep` gives it the STUCK/ETA signal.
- **No data migration.** Existing `invoices` rows are untouched (their `COMPLETED` means *document
  captured*); `orders` is populated going forward by the purchase domain.
