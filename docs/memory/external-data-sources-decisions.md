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

**Two avenues came out of it — NO CRs authored yet (user directive: "don't create CRs immediately"):**

1. **schema.org email-markup structured extraction — APPROVED direction (DN Decision 2).** Parse `Order` /
   `Invoice` / `ParcelDelivery` JSON-LD (+ legacy microdata) from the emails voa already fetches → map to
   `orders`/`invoices`/delivery fields. High-fidelity, deterministic, no new creds, portable; falls back to
   heuristic extraction when markup is absent; read-only + agent-mediated writes. **CR is DEFERRED** — author
   it when release-scoping is decided (candidate for a Wave 11 → 1.2.0, i.e. AFTER the current Wave 10 / 1.1.0).

2. **Carrier tracking via an aggregator — OPTION, opt-in, DECISION DEFERRED (DN Decision 3).** One AfterShip /
   EasyPost integration normalizes the whole mixed fleet (BlueDart / Delhivery / DTDC / India Post + DHL /
   FedEx) — pass tracking number + carrier slug (both come from mail or the Decision-2 `ParcelDelivery`
   markup), get a normalized status for `delivery-sweep`. Needs one **service** API key (not a carrier login).
   **Would ship opt-in / off-by-default (like the Mongo backend)** because it's an external network dependency
   + a privacy cost (tracking number + address leave the machine). **User will decide later whether to include
   it in a release** — revisit before scoping any CR.

Related: [[cicd-release-convention]] (wave/release model), and DN-mail-access (the mail spine these enrich).
