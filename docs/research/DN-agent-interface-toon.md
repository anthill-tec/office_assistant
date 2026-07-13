# DN — Agent interface: TOON output over the CLI (supersedes the MCP plan)

**Status:** Accepted (2026-07-12)
**Supersedes:** the "MCP interface" direction in PRD §8 and the original CR-OA-009 scope
**Author:** Antony John · **Co-author:** Vidushi (orchestrator — office-assistant)
**Related:** [PRD §8](PRD-lifecycle-domain-model.md) · [CR-OA-009](../changes/CR-OA-009-toon-output.md)

## Context

CR-OA-009 was specced as a thin **MCP server** wrapping the store verbs so the agent calls them as
parameterized tools instead of shelling out to `store.py` — to cut the per-call token overhead and offer
a native JSON query filter. Before building it, two facts reframed the decision:

1. the native JSON filter (`store.py query --filter '{…}'`) **already shipped in CR-OA-003**, so only the
   token-overhead goal remained; and
2. on this project's **Python 3.14** venv the official `mcp` SDK resolves to **~28 transitive packages**
   (pydantic, cryptography, uvicorn, starlette, httpx, jsonschema…), most for an HTTP transport we would
   never use — a heavy tree for a project whose only runtime dependency is `pymongo`.

## Decision

Drop the MCP server. Adopt the **AXI** stance — *Agent-eXperience Interface*: an agent-ergonomic CLI, not
a protocol server — and make the store's output **token-efficient by default**. `store.py` emits **TOON**
(Token-Oriented Object Notation) on every verb, with **`--json` a permanent fallback**.

TOON is a specced (v3.3, MIT), lossless, indentation + table encoding of the JSON data model: an array of
uniform objects declares its shape once (`[N]{field,…}:`) and lists rows as bare delimited values,
dropping the ~30–60% of tokens JSON spends on repeated keys, braces, and quotes. The store's reads are
overwhelmingly arrays of uniform rows — TOON's single best case.

### Why AXI/TOON over MCP

| | MCP server | TOON over the CLI (chosen) |
|---|---|---|
| Dependencies | ~28 transitive | **1** (`python-toon`) on top of pymongo |
| Tokens / task | ~185K | **~79K** (2.3× lighter) |
| Cost / task | $0.101–0.148 | **$0.050–0.074** |
| Task success | 82–99% | **100%** (AXI browser + GitHub suites) |
| Activation | user adds config + reloads a server | **nothing** — it's the CLI's own output |
| Reuses | a new runtime | **the CLI we already have** |

_(Figures: AXI's published 490-run benchmark, axi.md.)_

## Build vs buy — and which library

**Use an existing encoder, don't hand-roll one.** The encoder is where the spec's quoting edge-cases
live; tracking the spec is the library's job, not ours.

Library selection was **verified, not assumed** — the Python TOON ecosystem is young (all candidates are
0.1.x). Both leading candidates were installed and run against the spec's canonical example on the
project's Python 3.14 venv:

- **`toon_format`** (`pip install toon-format` — the name matching the spec authors' org) — its
  `encode()` raises `NotImplementedError: TOON encoder is not yet implemented`. A published **stub**
  (decode-only). Picking by name would have failed in GREEN.
- **`python-toon`** (`pip install python-toon`, imports as `toon`; xaviviro) — **encodes correctly to
  spec** on real store rows, runs on 3.14, round-trips (`encode`/`decode`). **Chosen.**

## Parameters (reviewed in the CR-OA-009 design surface, 2026-07-12)

- **Which reads default to TOON?** → **all read verbs** — the win applies wherever rows come back.
- **Do writes emit TOON?** → **`--format` is honored on every verb**; writes emit a TOON status object by
  default too, `--json` to opt out. One uniform rule beats a reads-only special case.
- **Pin tightness?** → **compatible floor `>=0.1.3,<0.2`** — patch fixes flow in; a format-shifting minor
  bump cannot.
- **Depend or vendor?** → **pip dependency**, declared + pinned in the project's dependency record.

## Consequences

- One new runtime dependency (`python-toon`), isolated behind a `scripts/toon.py` seam so it can be
  swapped or vendored later without touching call sites.
- The agent-facing default output changes JSON→TOON; the office-assistant skills read TOON going forward
  (Claude parses both). `--json` remains for any strict-JSON consumer or debugging.
- The `store.py` CLI stays the sole interface — no server to run, enable, or reload.

## Risks

`python-toon` is v0.1.x, single-maintainer, beta. **Mitigated structurally**: the `toon.py` seam, the
compatible pin, and the permanent `--json` fallback mean a bad release can't brick the store — fall back
to JSON, or swap the library behind the seam. We depend on it thinly, not deeply.
