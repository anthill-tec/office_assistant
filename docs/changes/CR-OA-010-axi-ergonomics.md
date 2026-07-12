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

> **Design decision (reviewed + approved 2026-07-12 — "B"): shape-changing ergonomics are TOON-only.**
> #2/#3/#4/#5/#9 reshape output, so they apply to the **TOON** (agent-facing default) view only. The
> **`--json` / `OA_FORMAT=json`** output stays a **clean, full-data array** — the stable programmatic
> contract — so no consumer is re-broken (the reconciled test suite needs no further changes). #7 (hook)
> and #8 (no-arg) are format-independent.

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
An empty TOON result shows an explicit zero-result marker (`count: 0`) rather than a bare `[]`, so
"nothing matched" is unambiguous. _(TOON envelope only — `--json` stays `[]`; see the design decision.)_

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
- [ ] §S1 in **TOON**, `query products` (no flags) shows only the `DEFAULT_FIELDS` for products; `--full` shows all fields; `--fields` overrides. Under `--json`/`OA_FORMAT=json` the same query returns the **full** documents (unchanged).
- [ ] §S2 in **TOON**, a record with a long `note` is truncated with a `(+N chars)` hint by default; `--full` shows the whole field. Under `--json` the field is never truncated.
- [ ] §S3 the **TOON** `query <type>` output carries a `count` equal to the number of results (matching `stats <type>.total` for an unfiltered query); the `--json` output is a bare array (no `count` wrapper).
- [ ] §S4 in **TOON**, a query matching nothing shows an explicit `count: 0` marker; under `--json` it stays a bare `[]`.
- [ ] §S5 the session hook (or skill) emits the `attention` worklist; installation is documented and testable.
- [ ] §S6 `store.py` with no args exits 0 and prints the `attention` summary + path + one-line description (not usage); `store.py --help` still shows help.
- [ ] §S7 in **TOON**, a read verb's output ends with a concise `next[]` block of relevant command templates; the `--json` output has no `next[]`.
- [ ] **(contract)** `--json` / `OA_FORMAT=json` output is **unchanged** by this CR for every read verb — a clean, full-data JSON array, no envelope / `count` / `next[]` / truncation — so the reconciled test suite needs no further edits.

## Estimated size
M–L — seven small ergonomics touches across the CLI output path + one session hook.

## Risk
The shape-changing ergonomics touch the **TOON** view only; the `--json` / `OA_FORMAT=json` contract is held
byte-stable (decision "B"), so **no JSON consumer is re-broken** — the whole point of the fork. The care
points: keep the TOON envelope (`count` / `results` / `next[]`) losslessly encodable via `oa_toon`, and make
the field-projection + truncation logic **format-aware** (minimal/truncated in TOON, full/verbatim in JSON).

## Non-goals
Re-implementing TOON (CR-OA-009); principles #6 and #10 (already met); a protocol server (dropped in the DN).
