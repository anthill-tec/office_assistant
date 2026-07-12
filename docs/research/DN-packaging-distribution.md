# DN — Packaging & cross-harness distribution (Vidushi OA)

**Status:** Accepted (2026-07-12), except the license decision (§6, pending)
**Author:** Antony John · **Co-author:** Claude (orchestrator — office-assistant)
**Related:** [PRD-distribution-release.md](PRD-distribution-release.md) · [DN-agent-interface-toon.md](DN-agent-interface-toon.md)

## Context

The store is mature and CLI-first. Reviewing the architecture + roadmap (2026-07-12), the user decided to
turn it into a distributable product and cut v0.1.0. These are the decisions that shaped the PRD.

## Decision 1 — Two distribution targets, layered (not either/or)

Packaging is **two composable targets**, not a single artifact:
- **The engine** — a Python package (`vidushi-oa`) exposing the `oa` CLI.
- **The operator skill** — a portable `vidushi-oa` skill that teaches an agent how to drive that CLI.

**Why layered:** the engine is the capability (deterministic, testable, versioned); the skill is the reach
(instructions any harness can load). They compose — install the engine, then the skill (which declares the
engine as a prerequisite). Asked directly "does the skill-distribution work with the pip engine install?":
yes, that's the design — the skill is the operator layer over the engine, not a replacement for it.

## Decision 2 — Skills-based cross-harness distribution (over MCP)

To reach **many** agentic harnesses, the roles ship as a **Vercel/skills-style** portable skill
(`npx skills add …` / a `SKILL.md` bundle), **not** an MCP server. Rationale: MCP was already dropped for
AXI/TOON (see [DN-agent-interface-toon.md](DN-agent-interface-toon.md)); a `SKILL.md` is a text artifact
every harness can consume, while MCP servers + subagents are harness-specific. (An MCP wrapper remains a
possible *later* add if a non-CLI harness ever needs one — not for v0.1.0.)

## Decision 3 — Unify the six skills into one; fold `inbox-analyst` as a mode

The six role-skills consolidate into a **single `vidushi-oa` skill**, and the `inbox-analyst` read-only
sweep is **folded in as a "deep-sweep" mode** rather than kept as a separate agent.

**Why fold, not keep separate:** subagents are a **harness-specific primitive** (Claude Code dispatches
them; many harnesses don't have the concept). A skill *mode* — a documented capability within the one
skill — ports to every harness, which is the entire point of Decision 2. In Claude Code the deep-sweep can
still be dispatched as a subagent under the hood; elsewhere it is simply a mode. (User raised this as an
open question on the diagram; recommendation accepted pending final confirmation.)

## Decision 4 — Rebrand to "Vidushi OA", minimal internal churn

Product name → **Vidushi OA**. The pip distribution is `vidushi-oa`, the import package `vidushi_oa`, the
unified skill `vidushi-oa`. **Kept for compatibility + low churn:** the console command `oa`, the env vars
`OA_*`, and the Mongo DB name `office_assistant` — all read naturally as *vidushi-OA*. Renaming those is an
explicit **non-goal** for v0.1.0 (a later, migration-bearing change if ever wanted).

## Decision 5 — A `setup` mode provisions the local MongoDB

A fresh `pip install` needs a reachable MongoDB. `oa setup` **checks the connection, guides provisioning,
and runs `init`** so the install "just works". It does not hard-code a connection — it complements the
existing `OA_MONGO_URI`/`OA_MONGO_DB` env resolution.

## Decision 6 — License gate (PENDING — the user's call)

Whether the repo stays **private** or migrates to an **open-source license** gates two things:
- **CI/CD** — a private repo won't run GitHub Actions on the free tier; OSS unlocks it.
- **Distribution** — private ⇒ private-index / git-install; OSS ⇒ public `pip install vidushi-oa`.

**Not decided yet.** v0.1.0 can ship **privately** (git-install, no CI) and go public later. This DN records
the tradeoff; the decision is a prerequisite for the deferred CI/CD work and any public publish.

## Consequences

- No PII ships in any package — `data/*.jsonl` + `documents/` stay local/chezmoi.
- The repo restructures (`scripts/*.py` → `vidushi_oa/`), so the in-repo `scripts/store.py` becomes a thin
  shim during migration to avoid breaking existing callers/tests until they're repointed.
- The six `~/.claude/skills/*` collapse into one distributable skill; the memory trackers + safety
  contract carry over into that one skill.

## Risks

Restructuring the package while keeping 122 tests green (path/import churn — mitigated by a compat shim and
per-cycle regression). The young `python-toon` pin still applies. The unified skill must not lose any of the
six roles' behaviour or the phishing/customs safety contract.
