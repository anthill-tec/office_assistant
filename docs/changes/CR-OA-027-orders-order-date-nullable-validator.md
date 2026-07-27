# CR-OA-027 — `orders.order_date` validator rejects null despite documented `str|null`

**Status:** PENDING
**Type:** bugfix
**Priority:** Medium
**Depends on:** 015
**Labels:** orders, schema, validator, doc-mismatch, bug
**Phase:** Wave 10 (embedded mail send)
**Design reference:** [data/schema.md](../../data/schema.md) (orders field table)
**Author:** Antony John · **Co-author:** Vidushi (orchestrator — office-assistant)

## Context

`data/schema.md:102` documents `orders.order_date` as **`str|null`** (`YYYY-MM-DD`), but the Mongo
`$jsonSchema` validator (`vidushi_oa/schema/orders.schema.json:25–27`) declares
`"order_date": {"bsonType": "string"}` — which **rejects null**. Recording an order whose date is not yet
known therefore **fails validation** unless the field is omitted entirely, contradicting the documented
contract (and the common "ordered, date unknown" case). Doc and validator must agree.

## Scope

### §S1 Make the validator accept null (align to the documented `str|null`)
`orders.schema.json` `order_date` accepts **string or null** — `"bsonType": ["string", "null"]` (matching the
nullable-field convention already used by other optional date fields in the schema validators). `data/schema.md`
stays `str|null`; the two now agree. After the change, `voa add orders --json '{… "order_date": null}'` and
`voa validate orders` both succeed.

The responses stay **AXI-conformant (CR-OA-017):** `voa add` returns the standard TOON status envelope (the
created id), and `voa validate orders` returns the **definitive empty state** (`[]`, AXI #5) when clean —
never a bare/ambiguous output — with the correct exit code.

## Acceptance criteria

### §S1
- [ ] `vidushi_oa/schema/orders.schema.json` `order_date.bsonType` is `["string", "null"]` (accepts both), not the bare `"string"`.
- [ ] `voa add orders --json '{"id":"ord_x", ..., "order_date": null}'` succeeds and `voa validate orders` reports it clean (`[]`) — asserted against the active backend in the test harness.
- [ ] **Regression:** a row with a string `order_date` (`"2026-07-01"`) still validates; a non-string/non-null value (e.g. a number) is still rejected.
- [ ] `data/schema.md` and the validator agree — the doc's `str|null` matches the validator's `["string","null"]` (no doc edit needed beyond confirming alignment).
- [ ] **AXI:** `voa validate orders` on a clean store returns the definitive empty state `[]` (AXI #5) with exit 0; `voa add orders … order_date:null` returns the TOON status envelope carrying the new id.

## Estimated size
XS — one validator field change + a null-accepted / string-still-valid test pair.

## Risk
Minimal — widening one field to accept null, matching the documented contract. No lifecycle/transition or FK
impact; `order_date` is not an anchor field for the `orders` id.

## Non-goals
Auditing every other field for doc/validator drift (a separate sweep if warranted); changing the `orders`
state machine or id-generation.
