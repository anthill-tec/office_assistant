# CR-OA-007 — `subscriptions` + `insurance` stores + memory migration

**Status:** COMPLETED (shipped 2026-07-12 on feature/CR-OA-007-subscriptions-insurance-stores)
**Type:** feature
**Priority:** Medium
**Depends on:** 002, 005, 006
**Labels:** mongo, new-store, migration
**Phase:** Wave 3
**Design reference:** PRD §2, §3, §6 (recurring domains, new stores, FK `subscription_id`)
**Author:** Antony John · **Co-author:** Claude (orchestrator — office-assistant)

## Context

Add the two recurring domains as first-class stores and migrate their records out of the memory
trackers into the store, so the state-machine engine and `attention` cover them.

## Scope

### §S1 Register the stores
Add `subscriptions` (`PREFIX sub`) and `insurance` (`PREFIX ins`) to `STORES`/`PREFIX`; `gen_id`
anchors (subscriptions → `provider`, insurance → `insurer`/`policy_no`). Ship their
`data/schema/*.schema.json` + `$jsonSchema` validators, unique `id` index, and transition maps
(recurring: IN_PROGRESS→DUE→IN_PROGRESS/COMPLETED). Add FK `subscription_id` → `subscriptions` in
`FK_MAP`.

### §S2 Migrate subscriptions (memory → store)
Migrate the subscription rows from `subscriptions-tracker.md` into `subscriptions` (fields: provider,
category, disposition KEEP/TOMBSTONE, plan, cadence, amount, currency, renews, alias, `status`,
`actions[]`, `documents[]`, source). The tracker holds 12 rows, but one (the **Maruti/HDFC Ergo
motor insurance**) is an insurance record migrated in §S3, so `subscriptions` receives **11**.

### §S3 Migrate the Ritz recurring records
Migrate the **HDFC Ergo** motor policy and the **RC re-registration** into `insurance`, both with
`product_id == "prod_maruti-suzuki_ritz-lxi"`.

### §S4 `due-sweep` verb (folded from CR-OA-005, 2026-07-12)
`store.py due-sweep` — for the recurring stores (`subscriptions`, `insurance`), find records whose
renewal window is reached (`renews`/`expiry` within a lookahead of today) and `status != "DUE"`, and
emit the `renewal-window` transition through the shared `_apply_transition` engine (IN_PROGRESS→DUE,
opening the domain's renew/cancel action per `transitions.py`). Idempotent (the `status != "DUE"`
filter skips already-DUE records); keep a `--dry-run`. The transition maps were shipped in CR-OA-005;
this adds the sweep that drives them (deferred here because the recurring stores didn't exist until §S1).

## Acceptance criteria
- [x] §S1 `store.STORES` includes `subscriptions` and `insurance`; after `init` each has a unique `id` index + a `$jsonSchema` validator; `store.py validate subscriptions` and `validate insurance` return `[]`.
- [x] §S2 `store.py stats subscriptions` `total == 11` (the tracker's 12 rows minus the Maruti motor-insurance row migrated to §S3 insurance); `get subscriptions sub_madmuscles` carries an OPEN `cancel-before-charge` action with `due` ≈ `2026-08-07`; `get subscriptions sub_signalrgb` reflects the cancelled lifecycle; every row's `disposition ∈ {KEEP, TOMBSTONE, UNDECIDED, CANCELLED}`.
- [x] §S3 `query insurance --where product_id=prod_maruti-suzuki_ritz-lxi --fields id,status` returns two rows: the HDFC Ergo motor policy (`status=="IN_PROGRESS"`, `expiry=="2027-05-05"`) and the RC registration (`status=="DUE"` with an OPEN `renew-registration` action).
- [x] §S1 (caller) `subscription_id` resolves via `get invoices <id> --expand subscription_id`.
- [x] §S4 a subscription with `renews` inside the lookahead + `status:"IN_PROGRESS"` → `store.py due-sweep` sets it `status:"DUE"` with an OPEN `cancel-before-charge` action; a far-future one is untouched; a second `due-sweep` adds no duplicate action (idempotent). `due-sweep` is a real subparser.

## Estimated size
M–L — 2 schemas + registration + faithful transcription of 13 subs + 2 Ritz records.

## Risk
Faithful transcription from memory (dispositions, renewal dates, the MadMuscles/SignalRGB/Ollama
states) — verify counts and spot-check named fields against the tracker before merge.

## Non-goals
Rewiring the `subscription-watch`/`purchase-tracker` skills to read the store (later skill
alignment); retiring the memory trackers.
