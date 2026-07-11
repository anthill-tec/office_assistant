# CR-OA-010 — AXI ergonomics (remaining principles)

**Status:** PENDING
**Type:** feature
**Priority:** Low
**Depends on:** 009
**Labels:** axi, cli, ergonomics
**Phase:** Wave 5
**Design reference:** PRD §8 (agent interface) · [DN-agent-interface-toon.md](../research/DN-agent-interface-toon.md) · axi.md (the 10 principles)
**Author:** Antony John · **Co-author:** Claude (orchestrator — office-assistant)

## Context

CR-OA-009 delivers AXI principle **#1** (TOON output). AXI ([axi.md](https://axi.md)) is **ten** principles;
`store.py` already meets **#6** (structured errors · idempotent · no prompts · exit codes) and **#10**
(`--help` per verb). This CR closes the remaining agent-ergonomics gaps so the interface is fully
AXI-aligned, not just token-efficient. **User-reviewed 2026-07-12** — #4/#5/#7/#8/#9 explicitly approved
for scheduling; #2/#3 folded in as the remaining efficiency principles.

## Scope

### §S1 Minimal default schemas (#2)
`query`/`get` return a per-store minimal default projection (a `DEFAULT_FIELDS` map, ~3–4 identifying
fields) unless `--fields` or `--full` is given. `--full` returns the whole document; `--fields` overrides.

### §S2 Content truncation + `--full` (#3)
Long string fields (e.g. `note`, `key_specs`) truncate to a length cap with a size hint (`…(+N chars)`);
`--full` disables truncation (shares the `--full` flag with §S1).

### §S3 Pre-computed aggregates (#4)
`query` output carries a derived `count` (and, where cheap, a by-status / by-acct tally) so the agent
doesn't round-trip a separate `stats` call.

### §S4 Definitive empty states (#5)
An empty result returns an explicit zero-result marker (`count == 0`) rather than a bare `[]`, so
"nothing matched" is unambiguous. _(This changes the `query` output envelope — see Risk.)_

### §S5 Ambient context hook (#7)
Ship a session hook (+ optional skill) that surfaces `attention` (the open-action worklist) before the
agent acts, so relevant state is ambient rather than fetched. Installation documented.

### §S6 Content-first, no-arg live data (#8)
`store.py` with no arguments prints live data (the `attention` summary) plus the executable path and a
one-sentence description — not the argparse usage. `store.py --help` still shows help.

### §S7 Contextual disclosure (#9)
Commands append a **concise** `next[]` block of suggested follow-up command templates after their output
(e.g. after a `query` that surfaces an OPEN action → an `action-resolve …` template). Kept terse.

## Acceptance criteria
- [ ] §S1 `query products` (no flags) returns only the `DEFAULT_FIELDS` for products; `--full` returns all fields; `--fields` still overrides.
- [ ] §S2 a record with a long `note` shows it truncated with a `(+N chars)` hint by default; `--full` shows the whole field.
- [ ] §S3 `query <type>` output includes a `count` equal to the number of results (matching `stats <type>.total` for an unfiltered query).
- [ ] §S4 a query matching nothing returns an explicit zero-result marker (`count == 0`), not a bare `[]`.
- [ ] §S5 the session hook (or skill) emits the `attention` worklist; installation is documented and testable.
- [ ] §S6 `store.py` with no args exits 0 and prints the `attention` summary + path + one-line description (not usage); `store.py --help` still shows help.
- [ ] §S7 a verb's output ends with a `next[]` list of concrete, relevant command templates.

## Estimated size
M–L — seven small ergonomics touches across the CLI output path + one session hook.

## Risk
Changing the default output shape (minimal fields, `count` envelope, empty-state marker, `next[]`) affects
any consumer parsing `store.py` output — the `--full` / `--fields` / `--json` escape hatches preserve full
access, and the office-assistant skills are updated to match. §S4's envelope layers **after** CR-OA-009's
byte-for-byte-array behaviour (which describes the pre-CR-010 output).

## Non-goals
Re-implementing TOON (CR-OA-009); principles #6 and #10 (already met); a protocol server (dropped in the DN).
