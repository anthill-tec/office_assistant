# CR-OA-005 — Transition-map state-machine engine + `event` verb

**Status:** PENDING
**Type:** feature
**Priority:** High
**Depends on:** 003, 004
**Labels:** mongo, state-machine, engine
**Phase:** Wave 3
**Design reference:** DN (deterministic state machine lives in the backend); PRD §3–§4
**Author:** Antony John · **Co-author:** Claude (orchestrator — office-assistant)

## Context

The deterministic transition logic is locked into the backend as a declarative table so the agent
only fires named events. Illegal transitions are rejected; automatic effects (open/close actions,
require a doc) run deterministically.

## Scope

### §S1 `scripts/transitions.py`
`TRANSITIONS[type]` = list of `{from, event, to, owner:"agent"|"user", effects:[…]}` where an
effect is `{op:"open-action", action, detail?, owner?, due?}` / `{op:"resolve-action", action}` /
`{op:"require-doc", type}`. Cover:
- **purchase** (`invoices`): `NEW→IN_PROGRESS` (`paid`/`shipped`), `IN_PROGRESS→COMPLETED` (`delivered`).
- **warranty**: `IN_PROGRESS→EXPIRED` (`expire`, agent) ⇒ open `renew-or-extend@user`;
  `EXPIRED→IN_PROGRESS` (`renew`).
- **recurring** (`subscriptions`/`insurance`): `IN_PROGRESS→DUE` (`renewal-window`, agent) ⇒ open
  the domain renew/cancel action; `DUE→IN_PROGRESS` (`renewed`); `DUE→COMPLETED` (`cancelled`/`lapsed`).

### §S2 `store.py event <type> <id> <event>`
Reads the doc's current `status`, finds the transition with matching `from==status` and `event`,
sets `to`, applies effects, bumps `updated`. An event with no matching transition returns
`{"error":"illegal transition", …}` and writes nothing.

### §S3 Sweeps through the engine
`warranty-sweep` re-expressed to emit `expire` for warranties whose `expiry < today`; a generalized
`due-sweep` emits `renewal-window` for recurring records whose `renews`/`expiry` falls inside a
lookahead window.

## Acceptance criteria
- [ ] §S2 `event invoices <id> delivered` on a doc with `status=="IN_PROGRESS"` sets `status=="COMPLETED"`; running it again (now COMPLETED, no matching transition) returns `{"error":"illegal transition",…}` and leaves the doc unchanged.
- [ ] §S1 `TRANSITIONS["warranties"]` contains an entry `{from:"IN_PROGRESS", event:"expire", to:"EXPIRED", owner:"agent"}` whose effects open `renew-or-extend`.
- [ ] §S3 Given a warranty with `expiry` before today, `warranty-sweep` sets `status=="EXPIRED"` and appends an OPEN `renew-or-extend` action (idempotent — a second sweep does not duplicate it).
- [ ] §S3 Given a subscription whose `renews` is within the lookahead window, `due-sweep` sets `status=="DUE"` and opens the domain renew/cancel action.
- [ ] **Caller:** `event` is a real subparser; `warranty-sweep`/`due-sweep` invoke the engine's `apply_transition` (grep confirms no duplicate transition logic).

## Estimated size
M–L — the transition tables + a small applier + two sweeps.

## Risk
Transition-table completeness — only mapped `(status,event)` pairs transition; everything else is
rejected. Keep effects idempotent so repeated sweeps don't duplicate actions.

## Non-goals
The new store definitions themselves (CR-OA-007); MCP exposure (CR-OA-009).
