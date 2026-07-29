# CR-OA-033 — Detect deployed-validator drift after an upgrade (`voa doctor` + remediation)

**Status:** PENDING
**Type:** maintenance
**Priority:** Medium
**Depends on:** 018
**Labels:** store, validators, upgrade, diagnostics, axi
**Phase:** Wave 11 (1.1.2 read-path patch)
**Design reference:** [DN-persistence-mongodb.md](../research/DN-persistence-mongodb.md) · [DN-mail-access.md](../research/DN-mail-access.md) §Decision 8 (`voa doctor` as detector + ordered remediation plan)
**Author:** Antony John · **Co-author:** Vidushi (orchestrator — office-assistant)

## Context

A field install of 1.1.1 reported that inserting an order with `order_date: null` is rejected by the
Mongo validator even though `data/schema.md` documents `order_date` as `str|null`. Investigation showed
the **shipped artifact is correct**: `vidushi_oa/schema/orders.schema.json` carries
`"bsonType": ["string", "null"]` (fixed in CR-OA-027, commit `0dada4d`) and `MongoBackend.provision`
already re-applies every validator unconditionally via `collMod`, so `voa init` *does* update an existing
collection.

The real defect is an **upgrade-safety gap**: upgrading the package does not re-provision the store, so a
long-lived deployment keeps running the **previous version's validators** until someone happens to re-run
`voa init` — and **nothing detects or reports that drift**. `voa doctor` reports the engine version, the
store backend and its reachability, the secret backend, and per-account credential resolution, but never
compares the *deployed* `$jsonSchema` validators against the *packaged* schemas. The user experiences it
as a phantom schema bug (a documented-and-shipped field rejected at runtime) with no diagnostic pointing
at the cause — exactly what happened here, costing a round-trip bug report.

## Scope

### §S1 Detect validator drift
The active backend gains a read-only comparison of each store's **deployed** validator against the
**packaged** schema, reporting per type: `ok` (identical), `drifted` (deployed differs), or `missing`
(no validator / no collection). It is a pure read — it never mutates the store. The comparison is
normalised (key order and equivalent encodings are not drift) so it cannot report false positives.
SQLite, which validates in-process against the packaged schema and cannot drift, reports `ok` uniformly.

**Surfaces (verified 2026-07-29):** `vidushi_oa/backends/mongo.py` `provision` (~line 179; the `collMod`
that *fixes* drift), `vidushi_oa/backends/base.py` (~line 45), `vidushi_oa/_cli.py` `cmd_doctor`
(~line 1344).

### §S2 Surface it in `voa doctor` with an ordered remediation step
`voa doctor` reports the drift state alongside its existing store/secret/account rows. When any type is
`drifted` or `missing`, doctor emits an **agent-runnable** remediation step — `voa init` — in the
ordered remediation plan of DN-mail-access §Decision 8, and the payload names the affected types.
Consistent with the existing contract, drift is a **reportable condition**, not a hard failure of an
otherwise-healthy store: exit stays 0 when everything else is fine, so a drifted validator is loud but
does not break automation that shells out to `doctor`.

### §S3 Documented upgrade step
The upgrade path states plainly that a version upgrade should be followed by `voa init` to re-apply
validators, and that `voa doctor` reports when it is needed.

## Acceptance criteria

### §S1
- [ ] With a collection whose deployed validator matches the packaged schema, the drift check reports
      `ok` for that type.
- [ ] After a deployed validator is replaced with a **stale** variant (e.g. `order_date` as
      `"bsonType": "string"`, the pre-CR-027 shape), the check reports that type as `drifted`; a
      collection with no validator reports `missing`.
- [ ] A validator that is semantically identical but differs in key order does **not** report drift.
- [ ] The check performs no writes: a `collMod`/`create_collection` spy records zero calls during
      `doctor`.
- [ ] On the SQLite backend the check reports `ok` for every type (no drift is representable).

### §S2
- [ ] `voa doctor` output carries a per-type validator state, and with a drifted type present the payload
      names that type and includes a `voa init` remediation step classified **agent-runnable**.
- [ ] With a drifted validator and everything else healthy, `voa doctor` exits **0** (reportable, not
      fatal); its existing non-zero conditions (store unreachable, account fails to resolve) are
      unchanged.
- [ ] After running `voa init`, a re-run of `voa doctor` reports every type `ok` and drops the
      remediation step — asserted end-to-end against a real Mongo store seeded with a stale validator.
- [ ] `voa doctor --json` remains a bare object (AXI decision-B).

### §S3
- [ ] The upgrade documentation states the `voa init` post-upgrade step and that `voa doctor` reports
      when it is required.

## Estimated size
S — a read-only validator comparison on the backend interface, a `doctor` row + remediation step, and a
documentation line.

## Risk
A naive comparison could report false drift (server-normalised validator representations differ from the
packaged JSON), which would train users to ignore the warning — the normalisation AC is the guard.
Reporting drift without auto-fixing is deliberate: silently mutating a deployed validator during a
read-only diagnostic would violate `doctor`'s read-only contract, so remediation stays an explicit
`voa init`.

## Non-goals
Auto-re-provisioning on upgrade or on `doctor` (remediation stays explicit); migrating **documents** that
violate a new validator; versioned/rolling schema migrations; changing any store schema.
