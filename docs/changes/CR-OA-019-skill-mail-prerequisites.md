# CR-OA-019 — Skill mail-access prerequisites (declared + orchestrated)

**Status:** PENDING
**Type:** docs
**Priority:** High
**Depends on:** 016
**Labels:** skill, mcp, prerequisites, mail, distribution
**Phase:** Wave 8 (distribution readiness)
**Design reference:** [DN-packaging-distribution.md](../research/DN-packaging-distribution.md) (Decision 8) · CR-OA-016 (the unified skill)
**Author:** Antony John · **Co-author:** Vidushi (orchestrator — office-assistant)

## Context

The unified skill reads mail through **MCP services** (FastmailMCP + a Gmail provider), but CR-OA-016
only mentions them inside the "Mailboxes & search" prose — it never **declares them as install
prerequisites**, so an installer doesn't know the skill needs them or how to set them up. Per
DN-packaging-distribution Decision 8, mail access is a **skill-level prerequisite** (the engine `voa`
is mail-agnostic) and is **declared + orchestrated**, not bundled — FastmailMCP is an installable MCP
server, while Gmail is the **claude.ai connector** (harness-specific, not a package). This CR makes the
prerequisites explicit and sets up the machine-readable + per-harness install.

## Scope

### §S1 Declared prerequisites in `SKILL.md`
Add a **Prerequisites** section that declares, gh-axi-style, everything the skill needs before use: the
**engine** (`uv tool install vidushi-oa`; MongoDB not required — SQLite default, per CR-OA-018), and the
**mail-access MCP services** — **FastmailMCP** (install + authenticate) and a **Gmail** provider (the
claude.ai connector on claude.ai/Claude Code; a different Gmail MCP elsewhere). State the graceful
degradation already in the skill: if a mailbox's MCP is unavailable, the skill **says so and continues
with whatever mailbox it can reach** (Fastmail-only fallback), never silently dropping coverage.

### §S2 Machine-readable MCP manifest (harness convenience)
Ship a machine-readable MCP declaration (e.g. `.mcp.json`) inside the skill bundle declaring the
**FastmailMCP** server (command/args as invoked), for harnesses that consume one (Claude Code). Verify
whether the Agent Skills / `npx skills` flat layout carries such a file portably; if it does not, place
it as a **documented harness-specific convenience** (clearly labelled), not a portability requirement.
The Gmail claude.ai connector is **not** manifest-declarable (OAuth in claude.ai) and is documented
instead (§S3).

### §S3 Per-harness setup docs
Add `skills/vidushi-oa/references/mail-setup.md` (linked from `SKILL.md`) documenting: installing +
authenticating **FastmailMCP**; authorizing the **claude.ai Gmail connector**; and the non-claude.ai
Gmail path (a substitute Gmail MCP). Cross-link from `README.md`'s install section so the mail
prerequisites sit beside the engine + skill install.

## Acceptance criteria

### §S1
- [ ] `SKILL.md` has a **Prerequisites** heading; within it `grep` finds `FastmailMCP`, a Gmail provider reference, and the engine install (`uv tool install vidushi-oa`); it states MongoDB is not required.
- [ ] The Fastmail-only graceful-degradation sentence is present (skill continues + says which mailboxes it reached when a mail MCP is missing).
- [ ] `agentskills validate skills/vidushi-oa` still exits `0`.

### §S2
- [ ] A machine-readable MCP manifest exists in the bundle declaring the FastmailMCP server; it parses as valid JSON with the server's `command`/`args`. If the flat layout can't carry it portably, it is present as a clearly-labelled harness-specific file and `SKILL.md`/`README.md` say so.

### §S3
- [ ] `skills/vidushi-oa/references/mail-setup.md` exists, is linked from `SKILL.md`, and documents FastmailMCP install+auth **and** the claude.ai Gmail connector authorization; a `grep` finds `FastmailMCP` and `connector`.
- [ ] `README.md`'s install section links the mail-setup reference alongside the engine + skill install.

## Estimated size
S — skill prose + one manifest + one reference file + a README link; grep/validate-gated. No engine code.

## Risk
The Agent Skills flat layout may not carry a `.mcp.json` portably — mitigated by §S2's fallback (a
labelled harness-specific file + docs). The exact FastmailMCP invocation is environment-specific —
resolved from the user's actual MCP config during execution, not guessed. Over-promising portability for
the claude.ai Gmail connector — mitigated by documenting it honestly as harness-specific.

## Non-goals
Abstracting mail behind a full provider interface (a larger, separate design — deferred); shipping mail
credentials in any artifact (never — the user authenticates each MCP); a non-claude.ai Gmail MCP
implementation (documented as a path, not built here).
