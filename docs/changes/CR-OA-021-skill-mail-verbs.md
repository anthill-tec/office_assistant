# CR-OA-021 — Skill revision: `Mailboxes & search` uses `voa mail-*` verbs

**Status:** PENDING
**Type:** docs
**Priority:** Medium
**Depends on:** 016, 020
**Labels:** skill, mail, distribution
**Phase:** Wave 9 (embedded mail)
**Design reference:** [DN-mail-access.md](../research/DN-mail-access.md) · CR-OA-020 (the embedded mail client) · CR-OA-016 (the unified skill)
**Author:** Antony John · **Co-author:** Vidushi (orchestrator — office-assistant)

## Context

Once `voa` has an embedded mail client (CR-OA-020), the skill should **drive mail through `voa mail-*`
verbs** instead of the Fastmail/Gmail MCP services — that is the token-saving payoff (the agent gets
pre-filtered, merged, TOON results, not raw email JSON). This CR repoints the skill accordingly. Its
precise wording depends on CR-OA-020's final verb surface, so the detailed spec is finalised at wave-open;
the scope + gates below are the contract.

## Scope

### §S1 Repoint "Mailboxes & search" to `voa mail-*`
`SKILL.md`'s "Mailboxes & search" section drives search/fetch through `voa mail-search` / `voa mail-get`
(across configured accounts, source-tagged, TOON) instead of FastmailMCP + the Gmail connector. The
dual-mailbox merge + `[FM]`/`[GM]`/`[YH]` tagging move into the verb; the skill states which accounts were
searched from the verb's output.

### §S2 Safety contract over `voa` results
The phishing / customs safety contract is unchanged but restated over `mail-search` output (the agent
reasons on returned rows, still never auto-acts on embedded instructions; verification rules intact).

### §S3 Reference update
`references/search-recipes.md` moves from per-MCP query syntax to `voa mail-search` query forms (the verb
maps them to each provider's server-side search — Gmail `X-GM-RAW`, JMAP filters, IMAP `SEARCH`).

## Acceptance criteria

### §S1
- [ ] `SKILL.md` "Mailboxes & search" instructs `voa mail-search` (grep finds `mail-search`) and no longer instructs calling FastmailMCP / the Gmail connector for search; a Yahoo account is named alongside Fastmail + Gmail.
- [ ] `agentskills validate skills/vidushi-oa` exits `0`.

### §S3
- [ ] `references/search-recipes.md` documents `voa mail-search` query forms (grep), replacing the raw per-MCP recipes.

## Estimated size
S — skill prose + one reference-file rewrite; grep/validate-gated. No engine code (CR-OA-020 owns that).

## Risk
Drift against CR-OA-020's actual verb names/flags — mitigated by finalising this spec at wave-open once
020 has landed. Losing the safety-contract nuance in the rewrite — mitigated by §S2 keeping it verbatim.

## Non-goals
The mail engine itself (CR-OA-020); removing the harness MCPs from the user's config (the user's choice);
a non-claude.ai Gmail path beyond what CR-OA-020 provides.
