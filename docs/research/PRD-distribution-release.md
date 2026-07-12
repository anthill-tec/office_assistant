# PRD — Vidushi OA: distribution & the v0.1.0 release

**Product:** Vidushi OA (repo `office_assistant`)
**Status:** Design contract (draft, 2026-07-12)
**Author:** Antony John · **Co-author:** Claude (orchestrator — office-assistant)
**Related:** [PRD-lifecycle-domain-model.md](PRD-lifecycle-domain-model.md) (the domain model) · [DN-packaging-distribution.md](DN-packaging-distribution.md) (the decisions) · [DN-agent-interface-toon.md](DN-agent-interface-toon.md)

## Context

The 10-CR program produced a mature system: a MongoDB-backed store (7 collections) fronted by an
AXI/TOON CLI (`store.py`, 21 verbs), operated by a roster of mail-tracking role-skills + one read-only
sweep agent. It runs from a `scripts/` folder via `python3 scripts/store.py` and its roles live in
`~/.claude/`. This PRD covers the next arc: **make it a distributable product and cut a v0.1.0 release.**

## Goals (v0.1.0)

1. **Rebrand** the product to **Vidushi OA** (the pip distribution + the unified skill are `vidushi-oa`;
   the short console command `oa` and the internal `OA_*` env / `office_assistant` DB name are kept — "OA"
   now reads as *vidushi-OA*).
2. **Package the engine** as an installable Python distribution (`pip install vidushi-oa` → an `oa`
   console command) with a **setup mode** that provisions/verifies a local MongoDB.
3. **Unify the roles** into a single, portable **`vidushi-oa` skill** distributable across many agentic
   harnesses (not only Claude Code).
4. Ship the two approved **AXI refinements**: disposition-aware `due-sweep` and an aggregate tally in the
   query envelope.

## §1 Rebrand → Vidushi OA

The product name becomes **Vidushi OA**. The pip distribution is `vidushi-oa`, the import package
`vidushi_oa`, the unified skill `vidushi-oa`. To minimise churn and preserve compatibility, the console
command stays `oa`, the env vars stay `OA_MONGO_URI`/`OA_MONGO_DB`/`OA_DATA_DIR`/`OA_FORMAT`, and the
Mongo DB stays `office_assistant` (all read naturally as *vidushi-OA*). Docs + CLAUDE.md carry the brand.

## §2 Two distribution targets (they compose)

Distribution is **layered, not either/or** (see the DN):

- **Target 1 — the engine:** the store as a Python package. `pip install vidushi-oa` + `oa setup`
  yields a working `oa` CLI backed by the user's own local MongoDB.
- **Target 2 — the operator skill:** a single portable **`vidushi-oa` skill** (a `SKILL.md` + assets)
  that teaches *any* harness's agent how to drive the `oa` CLI. Distributed Vercel/skills-style
  (`npx skills add …`). It declares the engine as a prerequisite.

Install order: **engine first** (pip + `oa setup`), **then** the skill. The skill is the reach; the engine
is the capability.

## §3 The engine package + setup mode

`scripts/*.py` → a `vidushi_oa/` package (`cli`/`mongo`/`transitions`/`toon` + `schema/*.json` as package
data); a `pyproject.toml` with the `oa` console entry and the pinned deps (`pymongo`, `python-toon`); and
an **`oa setup`** command that ensures a reachable local MongoDB (checks the connection, guides
provisioning, runs `init`). No PII ships — `data/*.jsonl` + `documents/` remain local/chezmoi.

## §4 The unified skill (+ deep-sweep mode)

The six role-skills (`subscription-watch`, `purchase-tracker`, `invoice-tracker`, `warranty-tracker`,
`product-catalogue`, `support-case-manager`) unify into **one `vidushi-oa` skill**. The `inbox-analyst`
read-only sweep is **folded in as a deep-sweep mode**, not a separate agent — because subagents are a
harness-specific primitive, whereas a skill *mode* ports everywhere (see the DN). In Claude Code the
deep-sweep can still be dispatched as a subagent under the hood.

## §5 v0.1.0 approved refinements

- **Disposition-aware `due-sweep`** — the renewal-window transition opens `renewal-confirm` for KEEP
  subscriptions and `cancel-before-charge` only for TOMBSTONE/UNDECIDED, so a live sweep is safe to run.
- **Aggregate tally in the TOON envelope** — the `query` envelope's `count` is joined by a cheap
  by-status / by-acct tally, so the agent skips a separate `stats` round-trip (finishing AXI #4).

## §6 Beyond v0.1.0 (roadmap, not in scope)

Deferred, in rough order: **reporting/export verb** (tax/expense + expiry calendar); **3rd mailbox —
Yahoo** (roll our own; the available mail MCPs are flaky); **attention TUI/dashboard** (needs a storyboard
first); **CI/CD** (blocked until the repo goes open-source — free-tier CI won't run on a private repo);
**MCP wrapper** (not required while the agent reads TOON; possibly our own, later).

## §7 Open decision — the license (gates §6 CI + §2 publish)

Private-repo today ⇒ no free CI and a **private-index / git-install** distribution. An **open-source**
migration unlocks GitHub Actions **and** public `pip install vidushi-oa`. This choice is a prerequisite
for enabling CI and for a public PyPI publish; v0.1.0 can ship privately and go public later.

## §8 Decomposition

The v0.1.0 work is decomposed into CRs in the queue [`../changes/README.md`](../changes/README.md):
CR-OA-011 (packaging + rebrand), CR-OA-012 (unified skill), CR-OA-013 (disposition-aware sweep),
CR-OA-014 (aggregate tally). Decomposition detail lives there, not in this contract.

## Non-goals (v0.1.0)

Renaming the Mongo DB / env vars (kept for compat); public PyPI publish (pending the license decision);
the §6 deferred features; a GUI/TUI.
