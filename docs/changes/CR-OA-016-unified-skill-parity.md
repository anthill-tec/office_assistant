# CR-OA-016 — Complete the unified `vidushi-oa` skill to supersede the legacy role-skills

**Status:** PENDING
**Type:** docs
**Priority:** High
**Depends on:** 012, 015
**Labels:** skill, distribution, supersede, parity
**Phase:** Wave 7 (unified-skill parity)
**Design reference:** [DN-purchases-persistence.md](../research/DN-purchases-persistence.md) · PRD-lifecycle-domain-model §3 · CR-OA-012 (the skill this completes)
**Author:** Antony John · **Co-author:** Vidushi (orchestrator — office-assistant)

## Context

CR-OA-012 shipped `skills/vidushi-oa/SKILL.md` as a conceptual superset of the six role-skills +
`inbox-analyst`, but a gap review found it is **not yet a complete drop-in**: the support domain
teaches a `cases.status` enum the validator **rejects** (`open → awaiting_support → …`, vs the
required `{NEW,UNKNOWN,IN_PROGRESS,COMPLETED,EXPIRED,DUE}`); the purchase domain points at a
**non-existent store**; `insurance` is only a name-drop while a live insurance row is **DUE**; and a
layer of operational detail (search recipes, carrier/category taxonomies, the calendar-reminder
recipe, per-domain report templates) was compressed out. This CR closes those gaps so the unified
skill can formally **supersede** the seven legacy `~/.claude/skills/` skills + the `inbox-analyst`
agent (pre-0.1.0 vestiges living outside the repo).

## Scope

### §S1 Support domain — shared lifecycle, valid status
The support domain drives `cases` with the shared status vocabulary
`{NEW,UNKNOWN,IN_PROGRESS,COMPLETED,EXPIRED,DUE}`; the legacy `open → awaiting_support → … → closed`
enum is removed. Case-specific stages become `actions[]` (`raise-ticket · rma-issue · ship-back ·
repair · replace · resolution-confirm`, per PRD §3). The worked `add` example uses a valid status.

### §S2 Purchase domain — wired to the `orders` store (CR-OA-015)
The purchase section names store **`orders`** (not "store type via order tracking"): the delivery
lifecycle is the order's `status` + `actions[]`, STUCK detection uses `voa delivery-sweep`, customs
sub-states are OPEN actions on the order, and a customs mail carrying only an AWB annotates/creates
an order. The placeholder is gone.

### §S3 Insurance — a first-class domain
Add an **Insurance / regulatory-renewal** domain section (store `insurance`): motor/health policy
renewal and vehicle **RC re-registration**, riding `DUE` via `voa due-sweep`, actions `renew-policy ·
pay-premium · renew-registration · fitness-test · kyc`, `product_id` FK to the insured asset — a real
section, not a clause under Subscription.

### §S4 Operational detail via progressive disclosure
Restore the compressed-out specifics as `skills/vidushi-oa/references/*.md` (progressive disclosure —
keeps `SKILL.md` under the ~500-line guideline), linked from the body:
- `references/search-recipes.md` — per-domain Fastmail single-phrase queries + Gmail rich queries.
- `references/carriers-and-customs.md` — carrier roster (Delhivery / DTDC / Blue Dart / India Post /
  Ekart / Shadowfax / FedEx / DHL / UPS / Aramex) + FPO / ICEGATE customs notes.
- `references/subscription-taxonomy.md` — the 2-part `provider-kind / service-kind` category tags +
  the never-tombstone-`finance/bank`-or-`security/password-manager` rule.
- `references/calendar-reminders.md` — the reminder recipe: default calendar id, `Asia/Kolkata`,
  all-day, `[sub-watch]` / `[buy-watch]` tags, recurrence-to-cadence, verify-after-write, and the
  **use `create_event`, not `compose_event`, in a headless terminal** caveat (flagged
  Fastmail/Claude-Code-specific, so the portable body stays harness-agnostic).
- `references/report-templates.md` — the per-domain report skeletons + urgency ladders + the invoice
  retrieval-tier order + the expense/tax (sum-by-`acct`/period/GST) view.

### §S5 Formal supersession
`CLAUDE.md` "Vidushi OA toolkit — roles" and `README.md` present the unified `vidushi-oa` skill as
the single installed role; the seven legacy `~/.claude/skills/` skills + the `inbox-analyst` agent
are marked **deprecated / decommissioned** with a one-line migration note (the deep-sweep mode
replaces the agent; the legacy files remain under `~/.claude/` outside the repo until the user
prunes them).

## Acceptance criteria

### §S1
- [ ] `grep -Eic 'awaiting_support|rma_issued|in_repair|status["\x27: ]+open' skills/vidushi-oa/SKILL.md` returns `0`; the support section names the shared status set and lists the case actions.
- [ ] The worked `cases` add example in `SKILL.md` **validates**: feeding its JSON to `voa add cases` against a throwaway DB raises **no** validator `WriteError`.

### §S2
- [ ] `grep -c 'store type via order tracking' skills/vidushi-oa/SKILL.md` returns `0`; the purchase section names store `orders` and references `delivery-sweep`.

### §S3
- [ ] `SKILL.md` has a dedicated Insurance domain heading; within that section `grep` finds `renew-registration`, `due-sweep`, and `product_id`.

### §S4
- [ ] `skills/vidushi-oa/references/` contains the five files named in §S4; `SKILL.md` links each; `agentskills validate skills/vidushi-oa` exits `0`.
- [ ] Restored-content greps pass: a carrier name (`Delhivery`) and `create_event` appear under `references/`, and the `compose_event` caveat text is present.

### §S5
- [ ] `CLAUDE.md` + `README.md` present the unified `vidushi-oa` skill as the role set; a deprecation/migration note for the seven legacy skills + `inbox-analyst` exists.
- [ ] Fidelity: every legacy capability maps to a domain section or a `references/` file — the coverage matrix in this Scope leaves no legacy feature unaccounted for.

## Estimated size
M — prose + five reference files + two roster docs; grep/validate-gated. No package code (the
`orders` store is CR-OA-015).

## Risk
Re-bloating `SKILL.md` past the ~500-line guideline — mitigated by pushing specifics to
`references/`. Losing a nuance again — mitigated by the fidelity AC (§S5) + grep gates.
Supersession is a doc/roster change; the legacy skill files physically remain in `~/.claude/skills/`
(outside the repo) until the user removes them.

## Non-goals
Deleting the legacy skill files from `~/.claude/` (outside the repo; the user prunes them);
per-harness install adapters beyond the `SKILL.md`; runtime portability tests.
