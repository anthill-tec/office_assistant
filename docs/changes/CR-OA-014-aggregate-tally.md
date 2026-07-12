# CR-OA-014 — Aggregate tally in the TOON query envelope

**Status:** PENDING
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
TOON-only (decision "B"); `--json`/`OA_FORMAT=json` stays a bare array.

## Scope

### §S1 A `tally` in the envelope
The TOON `query` envelope gains a **`tally`** — a `by-status` count map computed from the returned results
(no extra query): `{count, tally:{status:{<STATUS>:N,…}}, results, next}`. Where a store carries a natural
second axis cheaply (`acct` on all; `disposition` on subscriptions), include it too (`tally.acct`,
`tally.disposition`). Empty result → `count:0` with an empty `tally`. The `--json` output is unchanged
(a bare array, no `tally`).

## Acceptance criteria
- [ ] §S1 `query subscriptions` (TOON) → `from_toon(stdout)` has a `tally.status` map whose values sum to `count`, plus a `tally.disposition` map (subscriptions carry `disposition`).
- [ ] §S1 `query invoices` (TOON) → `tally.status` present and sums to `count`; `tally.acct` present.
- [ ] §S1 an empty query (TOON) → `count:0` and an empty/zero `tally` (no crash).
- [ ] **(contract)** `query <type> --json` / `OA_FORMAT=json` → a bare JSON array with **no** `tally` (decision B, byte-stable).

## Estimated size
S — a tally computed in `cmd_query`'s TOON envelope path.

## Risk
Keep the tally cheap (derive from the already-fetched results, no second Mongo round-trip) and TOON-only so
the `--json` contract stays byte-stable.

## Non-goals
Server-side aggregation pipelines; tallies on `--json`; tallies on non-`query` verbs.
