# CR-OA-013 — Disposition-aware `due-sweep`

**Status:** PENDING
**Type:** feature
**Priority:** Medium
**Depends on:** 007
**Labels:** state-machine, subscriptions, follow-up, v0.1.0
**Phase:** Wave 6 (v0.1.0)
**Design reference:** PRD-distribution-release §5 · PRD-lifecycle-domain-model §3 (recurring)
**Author:** Antony John · **Co-author:** Claude (orchestrator — office-assistant)

## Context

`due-sweep`'s `renewal-window` transition opens `cancel-before-charge` **uniformly** — wrong for a KEEP
subscription (which we want to *renew*, not cancel). Make the opened action disposition-aware so a live
sweep is safe to run on the migrated data (the reason no live sweep has run yet — CR-OA-007 follow-up).

## Scope

### §S1 Disposition-keyed renewal action
When a recurring record enters the renewal window (`IN_PROGRESS → DUE`), the opened action depends on the
record's `disposition`:
- `KEEP` → open **`renewal-confirm`** (protect / confirm the renewal),
- `TOMBSTONE` / `UNDECIDED` / `CANCELLED` (or unset) → open **`cancel-before-charge`** (today's behaviour).

Implement in the sweep/transition path (`cmd_due_sweep` or the `_apply_transition` effect for recurring
stores), reading the doc's `disposition`. Insurance (no disposition) keeps its `renew-policy` action.
Idempotent — a second sweep opens no duplicate. `renewal-confirm` is added to the subscriptions `ACTION_SETS`.

## Acceptance criteria
- [ ] §S1 a subscription with `disposition:"KEEP"` and `renews` inside the lookahead → `due-sweep` sets `status:"DUE"` with an OPEN **`renewal-confirm`** action and NO `cancel-before-charge`.
- [ ] §S1 a subscription with `disposition:"TOMBSTONE"` (or `UNDECIDED`) in the window → `due-sweep` opens **`cancel-before-charge`** (unchanged).
- [ ] §S1 an insurance record in the window still opens `renew-policy`.
- [ ] §S1 idempotent — a second `due-sweep` adds no duplicate action for either case.
- [ ] `renewal-confirm` is a member of `ACTION_SETS["subscriptions"]`.

## Estimated size
S — one disposition branch in the sweep + an action-set entry.

## Risk
Getting the disposition→action mapping right for the CANCELLED/unset edge cases; keep the sweep idempotent.

## Non-goals
Auto-running a live sweep (that's an operational step once this ships); changing the renewal-window trigger
(CR-OA-009 `renews`/`expiry`).
