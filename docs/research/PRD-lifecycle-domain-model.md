# Personal Asset & Subscription Lifecycle — Domain Model

> **Type:** PRD (design contract) · **Status:** ACTIVE
> **Author:** Antony John · **Co-author:** Vidushi (orchestrator — office-assistant) · 2026-07-11
>
> The authoritative WHY + WHAT for the asset & subscription lifecycle framework. CRs cite this;
> they do not re-derive design. The persistence choice + rationale live in
> [`DN-persistence-mongodb.md`](DN-persistence-mongodb.md); the decomposition into Change
> Requests lives in [`../changes/README.md`](../changes/README.md) — never here.
> Visual companion: [`../domain-model.html`](../domain-model.html).

## 1. Core idea

The assistant is a **lifecycle manager for the things the user owns or subscribes to.**
Every such thing is anchored, and over its life it accrues **domain objects** (purchase,
warranty, insurance, registration, service, subscription). Each domain object:

1. **wraps a minimum set of document assets + AI-scraped metadata** — the archived
   documents (invoice PDF, warranty card, policy schedule…) hold ground truth; the store
   row is the fast, queryable **metadata index** the agent scrapes off them;
2. runs a **domain-specific state machine** (shared lifecycle vocabulary, domain-specific
   action set + required documents);
3. advances only via **actions** — **every state transition is an action**, either
   **automatic** (agent) or **user-assisted** (needs the human).

## 2. The Catalogue — one registry, two kinds

The **catalogue (`products`) is the single registry** of everything owned or subscribed to.
Each entry declares a `kind`, and entries can group other entries:

| `kind` | What | Grouping relation | Example |
|---|---|---|---|
| **physical** | a tangible asset | **accessories** + **consumables** — children via `parent_id` | Maruti Ritz (+ stereo *accessory*, + engine-oil *consumable*); washing machine (+ detergent) |
| **virtual** | a digital / service product | **bundle** — grouped virtual items (`bundle_id`) | Ollama Pro; an Adobe CC bundle |

A physical product's children come in two flavours: **accessories** — durable add-ons that
*extend the asset's value* (a car stereo, the AMS Lite), usually one-time; and **consumables** —
replenished items (detergent, engine oil, printer filament) frequently bought on a
**subscription/recurring** model. So a physical asset can spawn recurring billing domains too,
through its consumables (`relation: accessory | consumable`, `billing: one-time | subscription`).

Every **domain object** (purchase, warranty, insurance, subscription-billing, service) hangs off
a catalogue entry via `product_id`; one `--expand` assembles the asset's full dossier. There is
**no separate anchor store** — a *subscription* is simply the **billing/renewal domain of a
`virtual` catalogue entry**, exactly as *insurance* is a recurring domain of a `physical` one.

A **virtual** entry carries a `billing` flag — **one-time** (Plex Lifetime, a Steam game, a
perpetual license → a single purchase, then COMPLETED) or **subscription** (recurring → carries
the subscription billing/renewal domain). Physical entries are effectively always one-time
purchases (plus their accessories).

## 3. Domains  (state machine × action set × minimum documents)

| Domain | Store | Lifecycle status | Action set (OPEN→RESOLVED) | Min. required docs |
|---|---|---|---|---|
| **Purchase / fulfilment** | `orders` | NEW→UNKNOWN→IN_PROGRESS→**COMPLETED** (delivered *or* a terminal side-state; the fine `stage` — Shipped/In transit/Out for delivery/Delivered, or Cancelled/Returned/Refunded/Delivery-failed — is carried separately) | payment · shipment · in-transit · out-for-delivery · delivery · customs-clearance · duty-payment · kyc · clarification · redelivery · return · refund · stuck-chase | order-confirmation |
| **Purchase document** *(proof of purchase)* | `invoices` | NEW→IN_PROGRESS→**COMPLETED** (document captured) | payment · tax-invoice · return · refund | purchase-order · invoice/receipt |
| **Warranty** | `warranties` | IN_PROGRESS (**ACTIVE**) → **EXPIRED** (from `expiry`) → renewed/closed | register-product · capture-serial · confirm-term · **renew-or-extend** · expiry-reminder · warranty-query | warranty-card · registration |
| **Insurance** *(new)* | `insurance` | IN_PROGRESS (ACTIVE) → **DUE** (renewal window) → RENEWED / LAPSED — **recurring yearly** | renew-policy · pay-premium · kyc · claim · price-compare | policy-schedule · renewal-notice · premium-receipt |
| **Registration / regulatory** *(new; e.g. vehicle RC)* | `insurance` or `registrations` | VALID → **DUE** → RENEWED / LAPSED — recurring | renew-registration · fitness-test · submit-form · pay-fee | RC / certificate · fitness-cert · fee-receipt |
| **Service / claim** | `cases` | OPEN → IN_PROGRESS → **RESOLVED/COMPLETED** | raise-ticket · rma-issue · ship-back · repair · replace · resolution-confirm | ticket · rma-authorization · service-report |
| **Subscription** *(new store; today in memory)* | `subscriptions` | NEW → IN_PROGRESS (active) → **COMPLETED** (cancelled/lapsed) | renewal-confirm · cancel-before-charge · keep/tombstone-decision · de-register-mandate · card-update · price-change · trial-end-cancel | receipt · mandate/SI-confirmation · cancellation |

**Shared status vocabulary:** `NEW · UNKNOWN · IN_PROGRESS · COMPLETED` (+ `EXPIRED` for the
warranty ACTIVE→EXPIRED refinement; recurring domains use `DUE` as their "renewal window" flag).
`null`/absent status ⇒ treated as `UNKNOWN`, and **not** enrolled in tracking.

## 4. Actions drive transitions

An `actions[]` entry = `{action, detail, status: OPEN→RESOLVED, opened, resolved?, owner: user|agent, due?}`.

- **OPEN = needs attention** — the universal keyword across every domain.
- Every **state transition has an action**. `owner:"agent"` = automatic (e.g. `warranty-sweep`
  flips ACTIVE→EXPIRED); `owner:"user"` = needs the human (e.g. resolving `renew-or-extend`).
- A **missing required document** (per §3) is itself an OPEN `archive-doc` action.

**Re-track rule:** each scan advances records in `{NEW, UNKNOWN, IN_PROGRESS, DUE, EXPIRED}`,
**skips** `COMPLETED`, and **always surfaces** any record with an OPEN action (`store.py attention`).

## 5. Document-asset wrapper

Each domain object carries `documents[]` = `[{type, path, number?, date?, status?}]` where
`type` is drawn from the domain's asset vocabulary (§3). The file lives under
`documents/<acct>/<vendor>/…`; the row holds the **scraped metadata** so the agent answers
"do I have the receipt / when does the policy renew" without opening the PDF. Each domain
declares a **minimum required set**; absence ⇒ an OPEN `archive-doc` action.

## 6. Data model changes

- **New store `subscriptions`** — migrate the 13 rows now in `subscriptions-tracker.md` memory
  into JSONL (id `sub_<provider>`), fields: provider, category, disposition (KEEP/TOMBSTONE),
  plan, cadence, amount, currency, renews, alias, `status`, `actions[]`, `documents[]`, source.
- **New store `insurance`** — policies (motor/health/…): insurer, policy_no, product_id (FK to
  the insured asset), premium, period, expiry, `status`, `actions[]`, `documents[]`, source.
  Motor-insurance + vehicle-registration for the Ritz move here from memory.
- **New FK `subscription_id`** (→ subscriptions) so purchases/insurance can point at a
  subscription anchor. Existing `product_id` anchors physical-asset domain objects.
- `products` gains `status` (the asset's own life: IN_PROGRESS while owned → COMPLETED when sold/retired).

## 7. Worked example — Maruti Ritz (physical anchor)

```
products/prod_maruti-suzuki_ritz-lxi   (the car — the physical asset)
 ├─ invoices     purchase doc(s)                         COMPLETED
 ├─ warranty     (n/a / lapsed for a used car)
 ├─ insurance    HDFC Ergo motor policy   ACTIVE, renews 2027-05-05   [renew-policy @user, yearly]
 ├─ registration RC re-registration       DUE (expired 2026-05-05)    [renew-registration @user  ← OPEN]
 └─ cases        service history (DAKSHINA) COMPLETED
```
A SaaS example (digital anchor): `subscriptions/sub_ollama` → its billing receipts (`invoices`)
and mandate doc hang off `subscription_id`; lifecycle IN_PROGRESS→COMPLETED (cancelled 2026-08-09).

## 8. Tooling & agent interface

**CLI (`scripts/store.py`) — parameterized verbs:**
`query`/`get`/`add`/`update`/`rm`/`stats` (data) · `set-status`/`action-add`/`action-resolve`/`doc-add`/`attention`
(lifecycle) · `event`/`warranty-sweep`/`due-sweep` (state machine) · `init`/`validate`/`import`/`snapshot` (admin).
Domain-agnostic — any new store (insurance, subscriptions) gets them for free once registered.

**Agent-facing output is token-efficient by default.** The interface goal is that the agent drives the
store with minimal per-call token overhead. Rather than a schema-heavy tool-protocol layer, the reads
emit **TOON** (Token-Oriented Object Notation — a lossless, indentation- and table-based encoding of the
JSON data model that declares an array's shape once and lists rows as bare delimited values, cutting the
~30–60% of tokens JSON spends on repeated keys, braces, and quotes). TOON is the **default** read format;
**`--json` is a permanent fallback** for any consumer that needs strict JSON. This is the AXI stance —
an agent-ergonomic CLI, not a protocol server. TOON is AXI principle **#1** (of ten, per axi.md) — the
largest token lever; the store already meets #6 (structured errors/idempotent/no-prompts/exit-codes) and
#10 (`--help`), and the remaining ergonomics (minimal default fields, truncation, pre-computed aggregates,
definitive empty states, an ambient-context hook, no-arg live data, contextual next-command hints) are
scheduled as **CR-OA-010**.

> **Direction change (2026-07-12):** an MCP server was the original §8 plan; it was dropped in favour of
> the TOON-output approach — lower per-task token cost, no HTTP/SDK dependency tree, and nothing for the
> user to enable or reload. The full rationale, the build-vs-buy call, and the library selection live in
> [`DN-agent-interface-toon.md`](DN-agent-interface-toon.md).

## 9. Decomposition

The breakdown of this design into implementation Change Requests is deliberately **not** fixed in
this contract — a PRD states WHY + WHAT, not the implementation plan. The current decomposition
(CR-OA-001 … CR-OA-009), their dependencies, waves, and status live in the CR queue
[`../changes/README.md`](../changes/README.md) and the `CR-OA-*` specs beside it.
