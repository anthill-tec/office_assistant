# CR-OA-017 — AXI conformance audit + gap-closure across the full verb surface

**Status:** COMPLETED (shipped 2026-07-26 on feature/CR-OA-017-axi-conformance-audit)
**Type:** maintenance
**Priority:** High
**Depends on:** 009, 010, 014
**Labels:** axi, conformance, toon, cli
**Phase:** Wave 8 (distribution readiness)
**Design reference:** [DN-agent-interface-toon.md](../research/DN-agent-interface-toon.md) · axi.md (the 10 principles) · gh-axi reference (github.com/kunchenguid/gh-axi)
**Author:** Antony John · **Co-author:** Vidushi (orchestrator — office-assistant)

## Context

AXI principles #1–#10 were implemented across CR-OA-009 (TOON output), CR-OA-010 (ergonomics #2–#5,
#7–#9), and CR-OA-014 (aggregate tally), and a conformance gate in `.skill-release.toml` asserts them.
Since then the canonical AXI spec (axi.md) and the `gh-axi` reference implementation have matured, so
the CLI may carry residual drift — principles verified on a few verbs rather than the **whole** verb
surface, an envelope field that differs from the canonical shape, or an error/exit-code detail that no
longer matches the spec. Distribution (CR-OA-018) should ship on a **fully** conformant, audited CLI.
This CR audits every `voa` verb against the matured spec and closes the gaps found.

## Scope

### §S1 Conformance audit (investigation)
Audit **every `voa` verb** — reads (`query`, `get`, `attention`, `stats`), writes (`add`, `update`,
`rm`, `set-status`, `action-add`, `action-resolve`, `doc-add`, `event`), sweeps (`warranty-sweep`,
`due-sweep`, `delivery-sweep`), and admin (`setup`, `init`, `validate`, `import`, `snapshot`,
`apply-validators`) — against the AXI 10 principles and the `gh-axi` envelope shape. Record the result
as a `### §S1 Findings` matrix (verb × applicable principle → PASS / GAP + evidence). Principle focus:
- **#1 TOON default / #decision-B** — every read emits TOON; `--json` yields clean JSON (bare array for
  lists, object for single/status) with **no** `tally`.
- **#2 minimal default fields** + `--full` widens; **#3 truncation** (`…(+N chars)`) + `--full` escape.
- **#4 aggregates** — `tally:` on collection reads; **#9 next-step** — `next[…]` after each output.
- **#5 definitive empty state** (`count: 0`); **#6 structured errors to STDOUT, exit 0/1/2, idempotent,
  no prompts** (verify the stream = stdout and the codes, per verb).
- **#8 content-first** (bare `voa` = live worklist, never `usage:`); **#10 per-subcommand `--help`**.

### §S2 Gap-closure
Fix every GAP recorded in §S1 so the AXI laws hold across the full verb surface, and **extend the
`.skill-release.toml` AXI gate** to assert the previously-unverified verbs (not just `query`/`event`).
Each fix is RED (a failing conformance test for the gap) → GREEN. No behavioural change to verbs that
already pass; the domain logic, schemas, and store are untouched — this is envelope/error/exit-code
conformance only.

### §S1 Findings (audit 2026-07-26, Mongo backend — conformance is above the backend seam)
Live-CLI audit against the 10 principles across the read/write/error/help surface.

**Conformant:** `query` (full `count`/`tally`/`results`/`next` envelope); `query --json` (bare array, **no**
`tally` — decision-B); `add` (TOON `added[]`/`skipped[]` status); `event` illegal transition (structured
`error` on **stdout**, exit 1, no traceback — #6); unknown flag (exit 2 — #6); bare `voa` (live `attention`
worklist, never `usage:` — #8); `--help` (subcommand list — #10).

**Gaps (drive §S2):**
| # | Verb | Gap | Principle |
|---|---|---|---|
| G1 | `get` (missing id) | returns `null` with **exit 0** — not a structured error, not exit 1 | #6 |
| G2 | `get` (success) | no `next[]` contextual disclosure | #9 |
| G3 | `attention` | bare `[0]:`/list — no `results`/`tally`/`next` envelope, no next-step | #4 / #9 |
| G4 | `stats` | bare object — no `next[]` | #9 |

Incidental (out of AXI scope — noted for a follow-up, **not** fixed here): `set-status`'s CLI signature is
`<type> <STATUS> --id …`, but `CLAUDE.md` documents `<type> <id> <STATUS>` — a doc drift.

## Acceptance criteria

### §S1
- [x] A `### §S1 Findings` matrix in this spec covers **every** verb above × each applicable principle, each row PASS or GAP with a one-line evidence pointer (command + observed output).

### §S2
- [x] Every read verb (`query`, `get`, `attention`, `stats`) emits a TOON envelope carrying `results[N]` (or the single object), `tally:`, and `next[N]`; a test parses each and asserts all three present.
- [x] `--json` on every verb returns valid JSON — a **bare array** for list reads, a single object for `get`/status/writes — and contains **no** `tally` key (decision-B); a test asserts `json_type` + key absence per verb.
- [x] A structured error (e.g. `get`/`event`/`update` on a missing id, and an unknown flag) is written to **STDOUT** with an `error` key and **no** `Traceback`; missing-target → exit `1`, unknown flag → exit `2`; a test asserts stream, key, and exit code.
- [x] Bare `voa` (no verb) prints live data (the `attention` worklist) and never the string `usage:`.
- [x] `voa <verb> --help` prints a per-subcommand reference for every verb (exit 0, names the verb).
- [x] The `.skill-release.toml` AXI conformance gate is expanded to exercise the read verbs beyond `query` and the write/error verbs enumerated above, and the full gate passes.

## Estimated size
S–M — an audit pass plus targeted envelope/error/exit-code fixes; no new domain behaviour, no store or
schema change. Size depends on the §S1 gap count.

## Risk
The audit may surface more drift than expected (each verb × 10 principles). Mitigated by the existing
gate as a baseline and by scoping §S2 strictly to conformance (no feature work). A `--json`/TOON
envelope change is agent-facing output — mitigated because the office-assistant skills read both and the
canonical shape is the target, not a bespoke one.

## Non-goals
New verbs or domain behaviour; the packaging/backend/licensing work (CR-OA-018); an MCP wrapper (AXI
supersedes it — DN-agent-interface-toon). Re-deriving AXI itself — the principles are settled; this only
verifies and closes conformance gaps.
