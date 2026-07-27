---
name: external-data-sources-decisions
description: Where voa can get richer order/delivery data — schema.org email extraction (approved, CR deferred) + carrier tracking aggregator (opt-in option, decision deferred)
metadata:
  type: project
---

Research + decisions on external data sources for order/purchase/delivery enrichment (2026-07-27).
Full design capture in [`../research/DN-external-data-sources.md`](../research/DN-external-data-sources.md).

**Finding:** no consumer-facing marketplace API exists — Amazon (SP-API = sellers, PA-API = affiliates) and
Flipkart (affiliate/seller only) expose nothing for a buyer's own order/purchase/warranty history; unofficial
scrapers need the user's marketplace password (rejected — never-enter-credentials). So **mail stays the data
spine**; login-gated pages stay a browser fallback.

**Two avenues came out of it:**

1. **schema.org email-markup structured extraction — IN THIS RELEASE → CR-OA-028, Wave 10 / 1.1.0 (user
   decision 2026-07-27).** Parse `Order` / `Invoice` / `ParcelDelivery` JSON-LD (+ legacy microdata) → map to
   `orders`/`invoices`/delivery candidates; read-only + agent-mediated writes; heuristic fallback when markup
   absent. **Scope reality:** the mail client fetches **headers only** today and `JmapAdapter.fetch_message`
   is unimplemented, so CR-028 must **add in-engine HTML body retrieval** (IMAP + JMAP), kept token-frugal by
   returning only compact candidates (body never reaches agent context) → an **L** feature, not a trivial parse.

2. **Carrier tracking via an aggregator — OPTION, opt-in, DECISION DEFERRED (DN Decision 3).** One AfterShip /
   EasyPost integration normalizes the whole mixed fleet (BlueDart / Delhivery / DTDC / India Post + DHL /
   FedEx) — pass tracking number + carrier slug (both come from mail or the Decision-2 `ParcelDelivery`
   markup), get a normalized status for `delivery-sweep`. Needs one **service** API key (not a carrier login).
   **Would ship opt-in / off-by-default (like the Mongo backend)** because it's an external network dependency
   + a privacy cost (tracking number + address leave the machine). **User will decide later whether to include
   it in a release** — revisit before scoping any CR.

Related: [[cicd-release-convention]] (wave/release model), and DN-mail-access (the mail spine these enrich).
