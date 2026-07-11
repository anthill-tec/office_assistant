# CR-OA-004 — Tracking verbs on pymongo

**Status:** PENDING
**Type:** feature
**Priority:** High
**Depends on:** 003
**Labels:** mongo, store, tracking
**Phase:** Wave 2
**Design reference:** PRD §3–§4 (status vocabulary, actions[], re-track rule)
**Author:** Antony John · **Co-author:** Claude (orchestrator — office-assistant)

## Context

The v1 tracking verbs (`set-status`, `action-add`, `action-resolve`, `doc-add`, `attention`)
exist on the JSONL store; port them onto pymongo with identical semantics.

## Scope

### §S1 `set-status <type> <STATUS> (--id | --where | --contains)`
Validate `STATUS ∈ STATUSES`; `$set` status + `updated` on the selected doc(s) (single or bulk).

### §S2 `action-add` / `action-resolve`
`action-add`: `$push` `{action, detail?, status:"OPEN", opened, owner?, due?}` (warn if the slug is
outside the type's `ACTION_SETS`). `action-resolve`: flip a matching OPEN action to `RESOLVED` and
stamp `resolved`.

### §S3 `doc-add <type> <id> <asset-type> <path>`
`$push` `{type, path, number?, date?}` to `documents[]` (warn if outside `DOC_ASSETS`).

### §S4 `attention [<type>]`
Return records with an OPEN action **or** an explicit `status ∈ {NEW, UNKNOWN, EXPIRED, DUE}`;
absent status is NOT flagged. Project `{type, id, name, status, open_actions}`.

## Acceptance criteria
- [ ] §S1 `set-status invoices COMPLETED --where vendor=Amazon.in` returns `{"status":"COMPLETED","count":N,"ids":[…]}` and sets status on N docs; `set-status invoices BOGUS --id X` returns `{"error":"invalid status",…}` and writes nothing.
- [ ] §S2 `action-add warranties war_fnirsi-2 capture-serial --detail "…"` appends an action with `status=="OPEN"` and an `opened` date; `action-resolve warranties war_fnirsi-2 capture-serial` sets that action's `status=="RESOLVED"` with a `resolved` date; resolving an already-RESOLVED/absent action returns `{"error":"no OPEN action",…}`.
- [ ] §S3 `doc-add invoices <id> invoice documents/personal/x/y.pdf` appends `{type:"invoice",path:…}` to `documents[]`.
- [ ] §S4 `attention` includes `war_fnirsi-2` with `open_actions == ["capture-serial","confirm-warranty-start"]`; it excludes COMPLETED docs with no OPEN action, and excludes every `contacts`/`products` row that has no `status` field.
- [ ] **Caller:** each verb is a real subparser; `attention` is the agent worklist entry point (grep confirms).

## Estimated size
M — five verb bodies ported to `$set`/`$push` + one aggregation-ish scan.

## Risk
`attention` must treat absent status as *not-flagged* (else every contact/product surfaces). `DUE`
belongs in the attention set for the recurring domains landing in CR-OA-007.

## Non-goals
The `event` transition engine + sweeps (CR-OA-005).
