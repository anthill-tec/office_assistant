# DN — Packaging & cross-harness distribution (Vidushi OA)

**Status:** Accepted (2026-07-12) — all decisions confirmed by the user, incl. §4 full rename + §6 license
**Author:** Antony John · **Co-author:** Vidushi (orchestrator — office-assistant)
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
still be dispatched as a subagent under the hood; elsewhere it is simply a mode. **(User confirmed 2026-07-12.)**

## Decision 4 — Full rename to "Vidushi OA" (hard cut) — CONFIRMED 2026-07-12

Product → **Vidushi OA**, a **full rename** — the user chose full-rename over minimal-churn and a **hard
cut** over back-compat aliases: pip `vidushi-oa`, package `vidushi_oa`, skill `vidushi-oa`, console
**`voa`**, env **`VIDUSHI_MONGO_URI`/`VIDUSHI_MONGO_DB`/`VIDUSHI_DATA_DIR`/`VIDUSHI_FORMAT`**, Mongo DB
**`vidushi_oa`** (migrated from `office_assistant`, test DB `vidushi_oa_test`). The old `oa`/`OA_*` names
are **dropped** (no aliases). The repo/folder name `office_assistant` stays (a separate git step). The live
DB migration runs via `snapshot`→`import` with count/validator verification before dropping the old DB (see
CR-OA-011 §S4).

## Decision 5 — A `setup` mode provisions the local MongoDB

A fresh `pip install` needs a reachable MongoDB. `oa setup` **checks the connection, guides provisioning,
and runs `init`** so the install "just works". It does not hard-code a connection — it complements the
existing `OA_MONGO_URI`/`OA_MONGO_DB` env resolution.

## Decision 6 — License gate (PENDING — the user's call)

Whether the repo stays **private** or migrates to an **open-source license** gates two things:
- **CI/CD** — a private repo won't run GitHub Actions on the free tier; OSS unlocks it.
- **Distribution** — private ⇒ private-index / git-install; OSS ⇒ public `pip install vidushi-oa`.

**Decided 2026-07-12: ship v0.1.0 privately (git-install), go public later.** CI/CD and a public PyPI
publish stay deferred until an open-source migration; v0.1.0 distributes privately (git-install).

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
