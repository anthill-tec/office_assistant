# CR-OA-028 — schema.org email-markup structured extraction (`voa mail-extract`)

**Status:** COMPLETED (shipped 2026-07-28 on 1.1.0)
**Type:** feature
**Priority:** High
**Depends on:** 020, 024, 026
**Labels:** mail, extraction, schema-org, jmap, imap, axi
**Phase:** Wave 10 (embedded mail send)
**Design reference:** [DN-external-data-sources.md](../research/DN-external-data-sources.md) §Decision 2 · [DN-mail-access.md](../research/DN-mail-access.md) §Decision 2 (transport), §Decision 5 (`mail-*` verbs)
**Author:** Antony John · **Co-author:** Vidushi (orchestrator — office-assistant)

## Context

Merchants embed **schema.org** structured data (JSON-LD / legacy microdata) in confirmation & shipping email
HTML — the markup Gmail reads for summary cards and package tracking. Parsing it gives **deterministic,
high-fidelity** order/invoice/delivery data with no marketplace API and no new credentials (DN
§Decision 2, approved). **But the current mail client fetches headers only** — `_HEADER_SPEC =
BODY.PEEK[HEADER.FIELDS …]` (`imap.py:20`), `Message` carries no body field, and
`JmapAdapter.fetch_message` raises `NotImplementedError` (`jmap.py:133`). So this CR must **add in-engine HTML
body retrieval** (a deliberate, scoped departure from DN-mail-access's headers-only *bounded projection*),
then parse the markup — while **keeping the token-frugal ethos**: the body is consumed **in-engine** and only
the **compact structured candidates** are returned to the agent (the raw body never enters agent context).

## Scope

### §S1 In-engine HTML body retrieval (token-frugal — body never surfaced raw)
Adapters gain a body-fetch path used **only** by extraction: **IMAP** fetches the `text/html` MIME part
(`BODY.PEEK[...]` / full-message fetch then MIME-parse to the HTML part); **JMAP** implements the
currently-unimplemented `fetch_message` via `Email/get` requesting `htmlBody`/`bodyValues`. The retrieved body
is passed straight to §S2 in-engine and is **not** added to the AXI mail row or returned raw — preserving the
bounded-projection intent (only §S3 candidates leave the engine).

### §S2 schema.org markup parser (email content is untrusted DATA)
Parse **JSON-LD** (`<script type="application/ld+json">`) and legacy **microdata** from the HTML, recognizing
schema.org **`Order`**, **`Invoice`**, **`ParcelDelivery`** (and nested `OrderItem` / `Product` /
`Organization` seller / an `Order`'s `potentialAction`/`ParcelDelivery`). The parser treats the email strictly
as **data**: pure JSON parsing, no code execution, and any imperative text inside the markup is **never** acted
on (injection-safe — the extractor emits candidates, it does not follow instructions).

### §S3 Map extracted entities → store candidates
Normalize to candidate rows matching the existing store schemas:
- `Order` → an **`orders`** candidate (order number, merchant/seller, items, order date, expected delivery,
  `orderStatus`) — and, when present, an **`invoices`** proof candidate.
- `Invoice` → an **`invoices`** candidate (number, total, date).
- `ParcelDelivery` → **`orders`** delivery fields (carrier, `tracking_number`, delivery address, expected
  arrival, delivery status).

### §S4 `voa mail-extract` verb — read-only, agent-mediated writes
`voa mail-extract --account <name> --uid <uid>` (the `(account, uid)` a `mail-search` row now carries via
CR-026) fetches the body (§S1), extracts (§S2), and returns the §S3 candidates as an **AXI TOON envelope**.
**No autonomous store writes** — candidates are surfaced for the agent to persist via the normal
`voa add/update` path (its `next[]` suggests the exact `add`/`update`). When the message carries **no markup**,
it returns the **definitive empty state** so the skill falls back to heuristic extraction (markup is an
enhancement, not a replacement).

### §S5 AXI conformance (CR-OA-017)
TOON envelope, `--json`, structured errors (no traceback), definitive empty state (#5), contextual `next[]`
(#9), exit codes.

## Acceptance criteria

Tests use **artificial** sample emails (the no-personal-data invariant — fictitious addresses/order numbers)
with embedded markup, driven through **fake adapters** (fake IMAP conn / fake JMAP transport) — no live mail.

### §S1
- [ ] The IMAP adapter can fetch a message's `text/html` part (asserted against a fake conn returning a multipart body); the JMAP adapter's `fetch_message` is **implemented** (no `NotImplementedError`) and requests `htmlBody`/`bodyValues` via `Email/get` (asserted against a fake transport).
- [ ] The retrieved raw body is **not** present in `mail-search`/`mail-get` output nor in `mail-extract`'s returned candidates (only structured fields leave the engine).

### §S2 / §S3
- [ ] An email whose HTML contains an `Order` JSON-LD block yields an `orders` candidate with the parsed order number, ≥1 line item, and `orderStatus` mapped to the store's status vocabulary (assert exact fields).
- [ ] An email with a `ParcelDelivery` JSON-LD block yields delivery fields incl. `tracking_number` and carrier.
- [ ] A **microdata** (non-JSON-LD) variant of the same `Order` extracts equivalently.
- [ ] Imperative text embedded in the markup (e.g. a `description` telling the agent to do X) is present only as an inert data field — no side effect; the extractor returns candidates only.

### §S4 / §S5
- [ ] `voa mail-extract --account <a> --uid <u>` against a fake adapter returns the candidates in a `{count, results, next}` TOON envelope; `next[]` contains a runnable `voa add orders …` / `update` built from a candidate.
- [ ] An email with **no** schema.org markup returns the definitive empty state (`count: 0`) — not an error — so heuristic fallback engages.
- [ ] A body fetch that genuinely fails live (an IMAP/network error, a JMAP method-level rejection, an unparsable 2xx body) or an adapter that cannot fetch a body at all exits 1 with the structured `{"error", "account", "uid"}` payload — never a raw traceback.
- [ ] **Caller-existence:** `voa --help` lists `mail-extract`, wired via a non-test `set_defaults` caller (grep ≥1).

## Estimated size
L — adds in-engine body retrieval across **both** transports (incl. implementing JMAP `fetch_message`), a
JSON-LD + microdata parser, entity→store mapping, and a new AXI verb.

## Risk
**Departs from headers-only bounded projection** — mitigated by consuming the body in-engine and returning
only compact candidates (token-frugal intent preserved; body never surfaced). **Untrusted email content** —
the parser is data-only and injection-safe; candidates are never acted on. **Markup ≠ truth / coverage
varies** — candidates are agent-confirmed, and absence degrades gracefully to heuristic extraction. **JMAP
body fetch** is new surface (thin-HTTP `Email/get`), riding CR-024's `Content-Type` fix.

## Non-goals
Carrier tracking aggregator (DN §Decision 3 — deferred by user decision). Autonomous store writes (candidates
only). HTML rendering, attachment parsing, non-schema.org formats. Threading changes.
