# CR-OA-009 — MCP interface

**Status:** PENDING
**Type:** feature
**Priority:** Medium
**Depends on:** 003, 004, 005
**Labels:** mcp, interface
**Phase:** Wave 4
**Design reference:** PRD §8 (MCP interface)
**Author:** Antony John · **Co-author:** Claude (orchestrator — office-assistant)

## Context

Expose the store verbs as parameterized MCP tools so the agent calls them directly (no per-call
CLI/token overhead), with the native JSON query filter available for nested/cross-domain queries.

## Scope

### §S1 `scripts/oa_mcp_server.py`
A pymongo-backed MCP server (stdio) advertising tools that wrap the verbs:
`store_query` (native `filter` doc + `fields` + `expand`), `store_get`, `store_add`,
`store_update`, `store_set_status`, `store_action_add`, `store_action_resolve`, `store_doc_add`,
`store_attention`, `store_event`, `store_validate`, `store_snapshot`, `store_due_sweep`. All
outputs suppress `_id`.

### §S2 Registration
Register the server as `oa-store` in the MCP config (project `.mcp.json` or the user config) so it
loads for the office-assistant project.

## Acceptance criteria
- [ ] §S1 The server starts and its tool list includes the 13 tools above; `store_query` with `{"type":"insurance","filter":{"status":"DUE"}}` returns the DUE insurance docs with no `_id` key.
- [ ] §S1 `store_attention` returns the same worklist set as the `store.py attention` CLI.
- [ ] §S2 The MCP config has an `oa-store` server entry pointing at `scripts/oa_mcp_server.py`; after reload the tools are advertised to the agent.

## Estimated size
M — one server module + a config entry.

## Risk
MCP registration is environment config — activation needs the user to enable/reload the server;
the CLI remains the fallback.

## Non-goals
Retiring the `store.py` CLI (kept as the scriptable/rollback path).
