# Office Assistant — Data Store Schema

All records live in **MongoDB** (db `office_assistant`, one collection per type), accessed via
`scripts/store.py` (never hand-parse — query through the script to save tokens). `store.py snapshot`
mirrors every collection to `data/*.jsonl` for chezmoi versioning; `store.py import` loads them back.
Common fields on every row: `id` (string, auto), `acct` (`personal`|`business`), `updated` (YYYY-MM-DD),
and the shared lifecycle triplet **`status` / `actions[]` / `documents[]`** (see *Lifecycle state model* below).
`source` pins the originating mail: `{"mailbox":"FM"|"GM","email_id":"...","thread_id":"...","attachment":"name.pdf"|null}`.

## Lifecycle state model
Every record carries a shared **`status`** plus a domain-specific **`actions[]`** set. Transitions are
locked into `scripts/transitions.py` and fired only by `store.py event` / the sweeps — the agent names
events, it never hand-rolls a state change.

**`status`** (enum, uppercase):
| value | meaning |
|---|---|
| `NEW` | just discovered, not yet worked |
| `UNKNOWN` | needs human clarification before it can advance |
| `IN_PROGRESS` | active (paid/shipped, warranty in force, subscription live) |
| `COMPLETED` | done (delivered / closed / cancelled-and-ended) |
| `EXPIRED` | warranties only — past term |
| `DUE` | recurring only — inside the renewal window, awaiting renew/cancel |

**`actions[]`** — pending/'done' work items, each `{"action": <slug>, "status": "OPEN"|"RESOLVED",
"owner": "user"|"support"|"agent", "opened": YYYY-MM-DD, "due"?: …, "detail"?: …, "resolved"?: …}`.
The **action set differs per domain** (that *is* the domain's state machine): warranties open
`renew-or-extend`, subscriptions `cancel-before-charge`, insurance `renew-policy`/`renew-registration`,
cases the RMA steps. `store.py attention` surfaces any row with an OPEN action or an attention-status.

**`documents[]`** — the scraped/saved artefacts backing the record, each `{"type": <slug>, "ref"|"file": …}`
(receipt PDF, policy schedule, mandate…); AI-extracted metadata off the source document lands here.

**Transitions** (`transitions.py`): purchase `NEW→IN_PROGRESS` (`paid`/`shipped`) `→COMPLETED` (`delivered`);
warranty `IN_PROGRESS→EXPIRED` (`expire` ⇒ open `renew-or-extend`) `→IN_PROGRESS` (`renew`); recurring
`IN_PROGRESS→DUE` (`renewal-window` ⇒ open the renew/cancel action) `→IN_PROGRESS` (`renewed`) /
`→COMPLETED` (`cancelled`/`lapsed`); order fulfilment `shipped`/`out-for-delivery` advance `stage` within
`IN_PROGRESS`, `delivered`→`COMPLETED`, customs events (`held-at-customs`/`duty-demanded`/`kyc-requested`/
`clarification-requested`) open the matching OPEN action, and `cancelled`/`returned`/`refunded`/
`delivery-failed` land `COMPLETED` with the terminal side-state recorded in `stage`. Illegal `(status,
event)` pairs are rejected and write nothing.

### Foreign keys & joins
Records reference each other by id. `store.py ... --expand <fk[,fk...]>` resolves them inline (adds
`<fk>_obj`), so one call does a rich query (e.g. product → its support contact → its invoice).
| FK field | → store | used on |
|---|---|---|
| `contact_id` | contacts | products (manufacturer support), warranties, cases, invoices |
| `invoice_id` | invoices | warranties, cases, products, **orders** |
| `warranty_id` | warranties | cases, products, invoices |
| `product_id` | products | warranties, cases, insurance, **orders** |
| `subscription_id` | subscriptions | invoices |
`contacts.kind` ∈ `reseller`|`manufacturer`|`service` distinguishes who a contact is. A product's
`contact_id` should point to a **manufacturer** (or service) contact, not the reseller it was bought from.
Example: `store.py get products prod_fnirsi_tmp-610s --expand contact_id,invoice_id,warranty_id`

## contacts — `vendor_contacts.jsonl`  (verified vendor support directory)
| field | type | notes |
|------|------|------|
| id | str | `ven_<vendor-slug>` |
| vendor | str | display name |
| kind | enum\|null | reseller \| manufacturer \| service |
| category | str | e.g. `electronics/pcb-fab` |
| acct | enum | personal \| business |
| support_email | str\|null | **verified** address only; null = `TBD` |
| portal | str\|null | support/returns URL |
| phone | str\|null | |
| account_ref | str\|null | customer/billing id |
| buying_alias | str\|null | the masked alias used to buy from them |
| rma_process | str\|null | how returns/claims are raised |
| verified_source | str | how the support contact was confirmed |
| notes | str\|null | |

## invoices — `invoices.jsonl`  (purchase **documents**: PO / invoice / receipt / credit note — the fulfilment/delivery lifecycle lives in `orders`)
| field | type | notes |
|------|------|------|
| id | str | `doc_<vendor>_<number-or-date>` |
| doc_type | enum | po \| invoice \| receipt \| creditnote |
| vendor | str | |
| number | str\|null | PO/invoice/receipt number |
| date | str | YYYY-MM-DD |
| amount | num\|null | total |
| currency | str | e.g. INR, USD |
| tax_amount | num\|null | GST/VAT portion |
| gstin | str\|null | seller GSTIN (business) |
| acct | enum | personal \| business |
| order_ref | str\|null | links to the order |
| products | [str] | line items (short) |
| file | str\|null | saved copy path under `documents/...`, or null if only pinned |
| warranty_id | str\|null | link to a warranties record |
| source | obj | mail pin (see above) |

## orders — `orders.jsonl`  (purchase **fulfilment** lifecycle — the delivery state machine; the proof-of-purchase document lives in `invoices`)
| field | type | notes |
|------|------|------|
| id | str | `ord_<merchant>_<number-or-date>` |
| merchant | str | who it was bought from (reseller/marketplace) — the id anchor |
| number | str\|null | order/reference number |
| items | [str] | line items (short) |
| amount | num\|null | order total |
| currency | str\|null | e.g. INR, USD |
| order_date | str\|null | YYYY-MM-DD |
| carrier | str\|null | shipping carrier |
| tracking | str\|null | AWB / tracking number |
| eta | str\|null | expected delivery YYYY-MM-DD (a past `eta` ⇒ `delivery-sweep` chases) |
| stage | str\|null | human-readable fine detail, distinct from `status`: `Ordered · Paid · Processing · Shipped · In transit · Customs clearance · Out for delivery · Delivered`, plus terminal side-states `Cancelled · Returned · Refunded · Delivery-failed` |
| last_event | str\|null | latest tracking-event text |
| last_event_date | str\|null | YYYY-MM-DD of the last event (>7 days ago ⇒ `delivery-sweep` chases) |
| alias | str\|null | masked buying alias / billing email |
| acct | enum | personal \| business |
| invoice_id | str\|null | FK→invoices (proof of purchase) |
| product_id | str\|null | FK→products |
| source | obj | mail pin (see above) |

**status:** the 4 shared values only — `NEW → UNKNOWN → IN_PROGRESS → COMPLETED` (no `EXPIRED`/`DUE`, since
fulfilment is not a recurring domain); `COMPLETED` = delivered **or** a terminal side-state (the flavour is in `stage`).
**action set:** `payment · shipment · in-transit · out-for-delivery · delivery · customs-clearance ·
duty-payment · kyc · clarification · redelivery · return · refund · stuck-chase`. The customs sub-states are
OPEN actions (`customs-clearance`/`duty-payment`/`kyc`/`clarification`), so `store.py attention` surfaces a
parcel awaiting the user; `delivery-sweep` opens `stuck-chase` on a stalled order.

## warranties — `warranties.jsonl`
| field | type | notes |
|------|------|------|
| id | str | `war_<vendor>_<product-slug>` |
| product | str | |
| vendor | str | |
| model | str\|null | | serial | str\|null | |
| purchase_date | str | YYYY-MM-DD |
| term_months | int\|null | coverage length |
| expiry | str | computed YYYY-MM-DD |
| extended | bool | extended warranty/AMC present |
| registration | obj | `{"required":bool,"deadline":str|null,"done":bool}` |
| acct | enum | personal \| business |
| invoice_id | str\|null | proof-of-purchase link |
| source | obj | mail pin |

## products — `product_catalogue.jsonl`  (owned-product knowledge base — keyed on the MANUFACTURER)
Focus is the **actual manufacturer** (the real maker), NOT the reseller/marketplace it was bought through.
Links must point to the **manufacturer's official** resources, never an intermediary's listing.
| field | type | notes |
|------|------|------|
| id | str | `prod_<manufacturer-slug>_<model-or-product-slug>` |
| product | str | what it is |
| manufacturer | str | **the maker** — the catalogue key |
| bought_from | str\|null | reseller/marketplace/intermediary (links to the invoice's `vendor`) |
| model | str\|null | | serial | str\|null | (usually on the warranty record) |
| category | str\|null | |
| kind | enum\|null | `physical` \| `virtual` (a service/digital good) |
| relation | enum\|null | `accessory` \| `consumable` (relative to a parent product) |
| billing | enum\|null | `one-time` \| `subscription` (how it's paid for) |
| links | obj | manufacturer-official only: `{product_page, manual, datasheet, support, drivers_firmware, spec_sheet, community}` (each url or null) |
| key_specs | str\|null | short spec summary |
| invoice_id | str\|null | proof-of-purchase link | warranty_id | str\|null | coverage link |
| contact_id | str\|null | FK → contacts (manufacturer support/service) |
| acct | enum | personal \| business |
| links_verified | str\|null | how/when the manufacturer links were confirmed (official domain) |
| source | obj | mail pin where the product was identified |
| notes | str\|null | |

## cases — `support_cases.jsonl`  (support / claim / RMA / service)
| field | type | notes |
|------|------|------|
| id | str | `case_<vendor>_<n>` |
| vendor | str | | product | str\|null | |
| warranty_id | str\|null | | invoice_id | str\|null | |
| issue | str | short summary |
| channel | enum | email \| portal \| phone |
| ticket | str\|null | vendor ticket/RMA number |
| status | enum | shared lifecycle: `NEW` \| `UNKNOWN` \| `IN_PROGRESS` \| `COMPLETED` (per-stage detail — awaiting-support, rma-issued, in-repair — now lives in `actions[]`) |
| opened | str | YYYY-MM-DD |
| last_contact | obj | `{"date":str,"by":"support"|"user"}` |
| next_action | str\|null | | owner | enum | user \| support |
| due | str\|null | next-action due date |
| deadline | str\|null | SLA / warranty-window deadline |
| threads | [obj] | `[{"mailbox":"FM"|"GM","thread_id":"..."}]` |
| log | [obj] | `[{"date":str,"note":str}]` append-only history |
| acct | enum | personal \| business |

## subscriptions — `subscriptions.jsonl`  (recurring billing / SaaS / memberships)
Recurring domain — rides the `DUE` status via `due-sweep` (renewal window on `renews`). **Disposition is user-owned.**
| field | type | notes |
|------|------|------|
| id | str | `sub_<provider-slug>` |
| provider | str | who bills (the id anchor) |
| category | str\|null | e.g. `SaaS / AI`, `media / streaming` |
| disposition | enum | **user-set**: `KEEP` \| `TOMBSTONE` \| `UNDECIDED` \| `CANCELLED` |
| plan | str\|null | plan/tier name |
| cadence | str\|null | annual \| monthly \| 30-day \| usage-based \| … |
| amount | num\|null | recurring charge |
| currency | str\|null | |
| renews | str\|null | next renewal/charge date (drives `due-sweep`) |
| alias | str\|null | masked buying alias / billing email |
| status | enum | shared lifecycle (recurring: adds `DUE`) |
| actions | [obj] | e.g. `cancel-before-charge`, `renewal-confirm` |
| documents | [obj] | receipt / mandate / cancellation |
| source | obj | mail pin |

## insurance — `insurance.jsonl`  (policies + regulatory renewals — starts subscription-like, renews yearly)
Recurring domain; an insurance record links the **insured asset** via `product_id` (e.g. a vehicle's motor policy or RC re-registration). Drives `due-sweep` off `expiry`.
| field | type | notes |
|------|------|------|
| id | str | `ins_<insurer-slug>_<policy_no>` |
| insurer | str | insurer / issuing authority (the id anchor) |
| policy_no | str\|null | policy or registration number |
| product_id | str\|null | FK → products (the insured asset) |
| premium | num\|null | |
| currency | str\|null | |
| period | str\|null | term description |
| expiry | str\|null | cover/registration end (drives `due-sweep`) |
| status | enum | shared lifecycle (recurring: adds `DUE`) |
| actions | [obj] | e.g. `renew-policy`, `renew-registration` |
| documents | [obj] | policy-schedule / renewal-notice / premium-receipt |
| invoice_id | str\|null | premium proof-of-purchase link |
| source | obj | mail pin |
