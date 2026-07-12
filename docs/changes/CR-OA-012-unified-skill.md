# CR-OA-012 — Unified `vidushi-oa` skill (cross-harness)

**Status:** PENDING
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
- [ ] §S1 `skills/vidushi-oa/SKILL.md` exists and its body references all six domains (grep each of `subscription`, `purchase`, `invoice`, `warranty`, `product`, `support`) **and** a `deep-sweep` mode; it contains no dependency on a separate `inbox-analyst` agent.
- [ ] §S1 the safety contract survives — grep the SKILL.md for the phishing/customs rule, `draft-then-confirm`, and verified-contacts-only.
- [ ] §S2 the SKILL.md names the `vidushi-oa` engine / `oa setup` as a prerequisite and instructs driving the store via the `oa` CLI (not raw Mongo, not MCP).
- [ ] §S2 the skill is harness-agnostic — no hard requirement on Claude-Code-only primitives (subagents named only as an optional Claude-Code optimisation for the deep-sweep).

## Estimated size
M — authoring one consolidated skill from six, plus the deep-sweep mode + install packaging.

## Risk
Losing a role's nuance or a safety rule in consolidation — mitigated by the grep-gated ACs and a diff
against the six source skills. Cross-harness portability is asserted structurally, not runtime-tested.

## Non-goals
An MCP server; per-harness adapters beyond a portable `SKILL.md`; retiring the engine CLI (the skill drives it).
