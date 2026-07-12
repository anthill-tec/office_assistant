# CR-OA-009 — TOON output for the store CLI (AXI interface)

**Status:** COMPLETED (shipped 2026-07-12 on feature/CR-OA-009-toon-output)
**Type:** feature
**Priority:** Medium
**Depends on:** 003, 004, 005, 007
**Labels:** axi, toon, interface, cli
**Phase:** Wave 4
**Design reference:** PRD §8 (agent interface) · [DN-agent-interface-toon.md](../research/DN-agent-interface-toon.md)
**Author:** Antony John · **Co-author:** Claude (orchestrator — office-assistant)
**Supersedes:** the MCP-server scope (dropped — see the DN)

## Context

Replaces the dropped MCP-server plan. The store speaks **TOON** (Token-Oriented Object Notation) by
default so the agent reads its output in ~30–60% fewer tokens than JSON, with `--json` a permanent
fallback. Encoding comes from the vetted `python-toon` library (the spec-org `toon_format` package is an
unbuilt stub — see the DN). This is the AXI stance: an agent-ergonomic CLI, not a protocol server.

## Scope

### §S1 `scripts/oa_toon.py` — the encoder seam
A thin shim over `python-toon`: `to_toon(obj) -> str` (and a symmetric `from_toon(s) -> obj` for tests /
round-trip). Isolates the one dependency behind our own module so it can be swapped or vendored later
without touching call sites. `python-toon` is declared as a project dependency, pinned compatible
`>=0.1.3,<0.2`.

### §S2 `--format toon|json` on `store.py`
Add a global `--format` option (values `toon`|`json`, **default `toon`**) honored by **every** verb —
reads (`query`, `get`, `stats`, `attention`, `validate`) *and* writes (`add`, `update`, `set-status`,
`action-add`, `action-resolve`, `doc-add`, `event`, the sweeps). Every stdout payload routes through
`toon.to_toon()` unless `--json` is given, in which case today's exact `json.dumps` output is preserved.
An **`OA_FORMAT`** env var (`toon`|`json`) sets the default when no flag is given — precedence `--format` >
`--json` shortcut > `OA_FORMAT` env > `toon` — so programmatic consumers can opt into JSON globally without
threading `--json` (consistent with the `OA_MONGO_*`/`OA_DATA_DIR` env pattern; garbage value → `toon`, no crash).

## Acceptance criteria

- [x] §S1 `scripts/oa_toon.py` imports cleanly; `to_toon([{…}])` returns a TOON string and
  `from_toon(to_toon(x)) == x` for the store's shapes; `python-toon` is pinned `>=0.1.3,<0.2` in the
  project's dependency record.
- [x] §S2 `store.py query subscriptions --fields id,provider,disposition,status,renews` with no
  `--format` emits a TOON block whose first line matches `^\[11[,]?\]\{id,provider,disposition,status,renews\}:`
  followed by 11 indented rows — **not** a JSON array.
- [x] §S2 The same query with `--json` emits a strict JSON array (byte-for-byte the pre-CR output).
- [x] §S2 **Lossless:** `from_toon(<toon output>)` deep-equals the `--json` object for the same query.
- [x] §S2 A write verb (`add`/`set-status`) emits a **TOON** status object by default and a JSON one
  under `--json` — i.e. `--format` is honored on writes too, not reads-only.
- [x] §S2 (caller) `--format` is a real global argparse option with `default="toon"`; grep confirms the
  read *and* write verbs route stdout through the `oa_toon.py` seam rather than a bare `json.dumps`.

## Estimated size

M — one shim module + threading a `--format` switch through the CLI's single output path.

## Risk

`python-toon` is v0.1.x/beta — mitigated by the `oa_toon.py` seam, the compatible pin, and the permanent
`--json` fallback (a bad release degrades to JSON, never bricks the store). See the DN.

## Non-goals

Retiring the JSON path (kept as `--json`, permanently); vendoring the library (chose a pinned pip
dependency); an MCP / HTTP server (dropped — see the DN).
