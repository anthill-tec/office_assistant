# CR-OA-014 — Aggregate tally in the TOON query envelope

**Status:** COMPLETED (2026-07-13)
**Type:** feature
**Priority:** Low
**Depends on:** 010
**Labels:** axi, toon, ergonomics, v0.1.0
**Phase:** Wave 6 (v0.1.0)
**Design reference:** PRD-distribution-release §5 · CR-OA-010 §S3 (the envelope) · axi.md #4
**Author:** Antony John · **Co-author:** Claude (orchestrator — office-assistant)

## Context

The TOON `query` envelope (`{count, results, next}`, CR-OA-010) carries a `count` but no breakdown —
finishing AXI #4 means adding a cheap **tally** so the agent doesn't round-trip a separate `stats` call.
TOON-only (decision "B"); `--json`/`VIDUSHI_FORMAT=json` stays a bare array.

## Scope

### §S1 A `tally` in the envelope
The TOON `query` envelope gains a **`tally`** — a `by-status` count map computed from the returned results
(no extra query): `{count, tally:{status:{<STATUS>:N,…}}, results, next}`. Where a store carries a natural
second axis cheaply (`acct` on all; `disposition` on subscriptions), include it too (`tally.acct`,
`tally.disposition`). Empty result → `count:0` with an empty `tally`. The `--json` output is unchanged
(a bare array, no `tally`).

## Acceptance criteria
- [x] §S1 `query subscriptions` (TOON) → `from_toon(stdout)` has a `tally.status` map whose values sum to `count`, plus a `tally.disposition` map (subscriptions carry `disposition`).
- [x] §S1 `query invoices` (TOON) → `tally.status` present and sums to `count`; `tally.acct` present.
- [x] §S1 an empty query (TOON) → `count:0` and an empty/zero `tally` (no crash).
- [x] **(contract)** `query <type> --json` / `VIDUSHI_FORMAT=json` → a bare JSON array with **no** `tally` (decision B, byte-stable).

## Close-out (2026-07-13)
RED (8 tests, `tests/test_cr_oa_014_tally.py`) → GREEN: a `_query_tally(docs)` helper adds a **`tally`** to the
TOON `query` envelope (`{count, tally, results, next}`) — a `by-status` map (missing→`UNKNOWN`, so it sums to
`count`) plus `acct`/`disposition` sub-maps when any doc carries them. Derived from the already-fetched
`docs` (pre-projection), so it's correct even when `--fields`/`_toon_shape` drops those fields from the
emitted rows — **no second Mongo query**. The `--json`/`VIDUSHI_FORMAT=json` path is untouched (bare array,
no tally — decision B, byte-stable). VERIFY: PASS, no blocking (only the `OA_FORMAT`→`VIDUSHI_FORMAT` spec
wording nit, fixed above). Final gate: **185/185 green**. Completes AXI principle #4.

## Estimated size
S — a tally computed in `cmd_query`'s TOON envelope path.

## Risk
Keep the tally cheap (derive from the already-fetched results, no second Mongo round-trip) and TOON-only so
the `--json` contract stays byte-stable.

## Non-goals
Server-side aggregation pipelines; tallies on `--json`; tallies on non-`query` verbs.
