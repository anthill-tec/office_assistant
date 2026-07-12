# CR-OA-012 — Unified `vidushi-oa` skill (cross-harness)

**Status:** COMPLETED (2026-07-13)
**Type:** feature
**Priority:** High
**Depends on:** 011
**Labels:** skill, distribution, release, v0.1.0
**Phase:** Wave 6 (v0.1.0)
**Design reference:** PRD-distribution-release §2, §4 · [DN-packaging-distribution.md](../research/DN-packaging-distribution.md) (Decisions 2, 3)
**Author:** Antony John · **Co-author:** Claude (orchestrator — office-assistant)

## Context

Consolidate the six role-skills + the `inbox-analyst` sweep into a **single portable `vidushi-oa` skill**
(a `SKILL.md` bundle) that teaches any agentic harness how to drive the `oa` engine — Vercel/skills-style,
not tied to `~/.claude/` and not an MCP server.

## Scope

### §S1 One skill, six domains + a deep-sweep mode
A `skills/vidushi-oa/SKILL.md` (in-repo, distributable) that covers all six domains — subscriptions,
purchases/deliveries+customs, invoices, warranties, product catalogue, support cases — plus a **read-only
deep-sweep mode** that replaces the separate `inbox-analyst` agent (per DN Decision 3: a skill mode ports
across harnesses; a subagent does not). It carries over the phishing/customs **safety contract**,
draft-then-confirm, verified-contacts-only, and the memory-tracker conventions.

### §S2 Engine-prerequisite + install path
The skill declares the **`vidushi-oa` engine** (the `oa` CLI) as a prerequisite and documents the install
order (engine via pip + `oa setup`, then the skill). It drives the store exclusively through `oa`/`store.py`
(TOON by default; `--json` when it needs to parse). Ship an install path (`npx skills add …` / a documented
copy step) and a harness-agnostic front-matter block.

## Acceptance criteria
- [x] §S1 `skills/vidushi-oa/SKILL.md` exists and its body references all six domains (grep each of `subscription`, `purchase`, `invoice`, `warranty`, `product`, `support`) **and** a `deep-sweep` mode; it contains no dependency on a separate `inbox-analyst` agent.
- [x] §S1 the safety contract survives — grep the SKILL.md for the phishing/customs rule, `draft-then-confirm`, and verified-contacts-only.
- [x] §S2 the SKILL.md names the `vidushi-oa` engine / `voa setup` as a prerequisite and instructs driving the store via the `voa` CLI (not raw Mongo, not MCP). *(The CR text predates CR-OA-011's rename; the deliverable correctly uses the shipped `voa` command.)*
- [x] §S2 the skill is harness-agnostic — no hard requirement on Claude-Code-only primitives (subagents named only as an optional Claude-Code optimisation for the deep-sweep).

## Close-out (2026-07-13)
RED→GREEN: a grep-gated test module `tests/test_cr_oa_012_skill.py` (23 assertions across the 4 ACs) →
authored `skills/vidushi-oa/SKILL.md` (180 lines) consolidating `mail-tracking-core` + the six role-skills
(subscription/purchase+customs/invoice/warranty/product/support) + the `inbox-analyst` sweep folded in as a
read-only **deep-sweep mode**. VERIFY ran a fidelity diff against all eight sources: every load-bearing
safety rule and each domain's nuance survived (phishing/customs double-trap, draft-then-confirm,
verified-contacts-only, never-invent-terms, KEEP/TOMBSTONE user-owned + never-tombstone-finance,
manufacturer-keyed catalogue, GST split, dual-mailbox `[FM]`/`[GM]` quirks, case state machine) — no
BLOCKING findings. Two SHOULD-FIX nuance losses in the deep-sweep (dropped `reply`/`draft`/`mark-read` from
the read-only list + the fail-closed rule) were restored; a Claude-Code-only `compose_event` quirk was
consciously left out of the portable skill. Final gate: **171/171 green** (148 + 23).

## Estimated size
M — authoring one consolidated skill from six, plus the deep-sweep mode + install packaging.

## Risk
Losing a role's nuance or a safety rule in consolidation — mitigated by the grep-gated ACs and a diff
against the six source skills. Cross-harness portability is asserted structurally, not runtime-tested.

## Non-goals
An MCP server; per-harness adapters beyond a portable `SKILL.md`; retiring the engine CLI (the skill drives it).
