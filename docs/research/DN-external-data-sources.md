# DN — External data sources for order / purchase / delivery enrichment

**Status:** ACTIVE
**Author:** Antony John · **Co-author:** Vidushi (orchestrator — office-assistant)
**Context date:** 2026-07-27

## Context

voa's data source is the user's **mail** (Fastmail + Gmail), read through the embedded `mail-*` client
(DN-mail-access). The open question: can voa obtain **richer, more structured** order / purchase / delivery /
warranty data than free-text mail parsing — ideally via a direct vendor API? This DN records the research and
the resulting design decisions on *where* that data can come from.

## Decision 1 — No consumer marketplace API exists; mail stays the spine (finding)

Researched 2026-07-27. **Amazon and Flipkart expose no consumer-facing API for a buyer's own
order/purchase/warranty history.** Every official API serves **sellers or affiliates**:

- **Amazon SP-API** — sellers (their fulfilled orders, inventory, payouts). **PA-API 5.0** — affiliates
  (catalog/product/price for promotion; Python: `python-amazon-paapi`). Neither returns the buyer's purchases.
- **Flipkart** — Affiliate API (being deprecated; its "Order Report" is affiliate-driven orders) + a
  seller/marketplace API. No consumer order API.
- **Warranty** is a **manufacturer** concern, not the marketplace's — registration portals, rarely any open
  API (voa already keys products on the manufacturer).
- **Unofficial scrapers** (e.g. the `amazon-orders` Python lib) parse the consumer site but **require the
  user's marketplace email + password** — **rejected**: it violates the never-enter-credentials rule, is
  `.com`-only, fragile, and ToS-gray.

**Consequence:** the data spine remains **the user's mail**, enriched by Decision 2 (structured email markup)
and, optionally, Decision 3 (carrier tracking). Login-gated marketplace pages stay a **browser-automation**
fallback (user logs in; agent reads; never enters credentials) — DN-mail-access's supporting-capabilities
stance.

Sources: Amazon SP-API / PA-API (sarasanalytics.com, en.wikipedia.org/wiki/Amazon_Product_Advertising_API);
Flipkart affiliate docs (affiliate.flipkart.com/api-docs); amazon-orders (amazon-orders.readthedocs.io).

## Decision 2 — schema.org email-markup structured extraction (APPROVED 2026-07-27)

Merchants embed **schema.org structured data** (JSON-LD, and legacy microdata) inside confirmation/shipping
email HTML — the same markup Gmail reads for its summary cards and package tracking. The relevant types:

- **`Order`** — order number, line items, prices, seller, order/expected-delivery dates, `orderStatus`.
- **`Invoice`** — billing / total / payment reference (proof-of-purchase).
- **`ParcelDelivery`** — carrier, `trackingNumber`, `deliveryAddress`, `expectedArrivalUntil`, delivery status.

**Decision:** voa parses this markup from the emails it **already fetches** and maps the entities onto its
stores (`orders` / `invoices` / delivery fields). This is **high-fidelity, deterministic extraction with no
new credentials and no external dependency** — a perfect fit for the "read mail → structured store" model,
and fully portable. Where markup is **absent**, voa falls back to the existing skill/LLM heuristic extraction
— markup is an **enhancement, not a replacement**. Extraction is **read-only and agent-mediated**: it returns
structured candidate rows (AXI TOON); writes still go through the normal `voa add/update` path the agent
drives (no autonomous store writes). Implemented by **CR-OA-028** — **added to this release (Wave 10 → 1.1.0)
per user decision 2026-07-27.**

> **Scope reality:** the mail client currently fetches **headers only** (bounded projection) and
> `JmapAdapter.fetch_message` is unimplemented, so CR-OA-028 must **add in-engine HTML body retrieval** across
> IMAP + JMAP. Token-frugality is preserved by consuming the body **in-engine** and returning only the compact
> candidates — the raw body never reaches agent context. This makes it an **L** feature, not the trivial parse
> it first appeared.

Sources: schema.org email markup / Gmail (mailslurp.com/guides/email-schema, structured.email).

## Decision 3 — carrier tracking via an aggregator (OPTION — opt-in, pending user decision)

Delivery status across a **mixed logistics fleet** — domestic **BlueDart / Delhivery / DTDC / India Post** +
international **DHL / FedEx** — is tractable through a **multi-carrier aggregator** rather than N per-carrier
integrations:

- **One integration.** Call the aggregator (**AfterShip** or **EasyPost**, both with Python SDKs) with a
  `tracking_number` (+ optional carrier `slug`, else auto-detected). It maintains **1,100+ carrier
  integrations** (incl. all of the above) and returns a **normalized** status (AfterShip: 7 statuses / 33
  sub-statuses) that maps onto voa's `orders` delivery state machine (feeds `delivery-sweep`).
- **Inputs from mail** — the tracking number + carrier come straight from the shipping email (or, cleanly,
  from Decision 2's `ParcelDelivery` markup).
- **Auth** — one **service API key** in voa's secret store; **never a carrier login**.
- **Direct alternative** — DHL (Unified Tracking API) and FedEx (Track API) have official APIs; but the Indian
  domestic carriers mostly expose only partner/enterprise APIs, so direct = fragmented N-integrations. The
  aggregator collapses that.

**Why opt-in, not default (pending):** it is an **external network dependency** (against voa's lean/local
ethos) and a **privacy cost** (each call sends a tracking number + often the delivery address to a third
party). So — **like the Mongo backend — it would ship opt-in, off by default**, and only if the user elects
it. **No CR authored yet**: awaiting a user decision to pursue it.

Sources: AfterShip (aftership.com/tracking-api, carriers.aftership.com/bluedart, .../dtdc,
aftership.com/carriers/india-post/api); EasyPost (easypost.com/tracking-api); DHL
(developer.dhl.com/api-reference/shipment-tracking); FedEx (developer.fedex.com/api/en-us/catalog/track.html).

## Consequences

- The data spine stays **mail** — no marketplace-API dependency, no marketplace credentials.
- **Decision 2 (schema.org)** is a pure, portable fidelity upgrade over free-text parsing → CR-OA-028
  (Wave 11 → 1.2.0).
- **Decision 3 (carrier tracking)** is a documented **opt-in option**, not committed — recorded so a future
  wave can scope a CR if the user elects it.

## Risks / open questions

- **Markup coverage varies** by merchant — Decision 2 must degrade gracefully to heuristic extraction (no
  regression when markup is absent).
- **Markup ≠ truth** — schema data can drift from the human-readable email; treat extracted rows as
  **candidates** for the agent to confirm, consistent with voa's agent-mediated writes.
- **Decision 3 privacy** — sending tracking numbers/addresses off-machine needs explicit opt-in; document it
  plainly (as with the bootstrap-secret truth in DN-mail-access).
