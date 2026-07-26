# CR queue — Vidushi OA (repo `office_assistant`, code `OA`)

Single source of truth for Change-Request **process state**: status, dependencies, wave, and
ordering. Pick the next `PENDING` CR whose dependencies are all `COMPLETED`. Status lives here —
never inside a CR spec file.

- **Design contracts:** [`PRD-lifecycle-domain-model.md`](../research/PRD-lifecycle-domain-model.md) (domain model) · [`PRD-distribution-release.md`](../research/PRD-distribution-release.md) (packaging + v0.1.0)
- **Decision notes:** [`DN-persistence-mongodb.md`](../research/DN-persistence-mongodb.md) · [`DN-agent-interface-toon.md`](../research/DN-agent-interface-toon.md) · [`DN-packaging-distribution.md`](../research/DN-packaging-distribution.md) · [`DN-purchases-persistence.md`](../research/DN-purchases-persistence.md)
- **Canonical states:** `PENDING` · `IN_PROGRESS` · `COMPLETED` · `SUPERSEDED` · `DEFERRED`.

## Execution model — Solo single-orchestrator (no Mainline / parallel Tracks)

One orchestrator executes the queue **sequentially** by dependency/wave order — no Model-B
Mainline + parallel Track workers, no Sandesh coordination. Two-phase per the house convention:

- **Design phase → `main`** (integration branch): this queue, the PRD, the DN, and the CR specs
  themselves commit to `main`.
- **Execution phase → a per-CR feature branch**: each CR gets its own branch
  (`feature/CR-OA-NNN-<slug>`), RED→GREEN→VERIFY against its ACs, then merge to `main`. Only one
  CR is `IN_PROGRESS` at a time.

## Queue

| CR | Title | Type | Status | Depends on | Wave |
|---|---|---|---|---|---|
| [CR-OA-001](CR-OA-001-mongo-connection.md) | Mongo connection & collection bootstrap | feature | COMPLETED (2026-07-11) | — | 1 |
| [CR-OA-002](CR-OA-002-schema-validators.md) | Domain JSON-Schema validators + `validate` | feature | COMPLETED (2026-07-11) | 001 | 1 |
| [CR-OA-003](CR-OA-003-store-crud-pymongo.md) | `store.py` CRUD on pymongo (CLI-compatible) | feature | COMPLETED (2026-07-11) | 001 | 2 |
| [CR-OA-004](CR-OA-004-tracking-verbs-pymongo.md) | Tracking verbs on pymongo | feature | COMPLETED (2026-07-11) | 003 | 2 |
| [CR-OA-006](CR-OA-006-migration-and-snapshot.md) | Migration `import` + `snapshot` versioning | feature | COMPLETED (2026-07-11) | 003 | 2 |
| [CR-OA-005](CR-OA-005-state-machine-engine.md) | Transition-map state-machine engine + `event` | feature | COMPLETED (2026-07-12) | 003, 004 | 3 |
| [CR-OA-007](CR-OA-007-subscriptions-insurance-stores.md) | `subscriptions` + `insurance` stores + memory migration | feature | COMPLETED (2026-07-12) | 002, 005, 006 | 3 |
| [CR-OA-008](CR-OA-008-docs-and-rules.md) | Docs & rules refresh | docs | COMPLETED (2026-07-12) | 001–007 | 4 |
| [CR-OA-009](CR-OA-009-toon-output.md) | TOON output for the store CLI (AXI interface) | feature | COMPLETED (2026-07-12) | 003, 004, 005, 007 | 4 |
| [CR-OA-010](CR-OA-010-axi-ergonomics.md) | AXI ergonomics — remaining principles (#2–#5, #7–#9) | feature | COMPLETED (2026-07-12) | 009 | 5 |
| [CR-OA-011](CR-OA-011-packaging-rebrand.md) | Packaging + rebrand to Vidushi OA | feature | COMPLETED (2026-07-12) | 010 | 6 · v0.1.0 |
| [CR-OA-012](CR-OA-012-unified-skill.md) | Unified `vidushi-oa` skill (cross-harness) | feature | COMPLETED (2026-07-13) | 011 | 6 · v0.1.0 |
| [CR-OA-013](CR-OA-013-disposition-aware-sweep.md) | Disposition-aware `due-sweep` | feature | COMPLETED (2026-07-13) | 007 | 6 · v0.1.0 |
| [CR-OA-014](CR-OA-014-aggregate-tally.md) | Aggregate tally in the TOON envelope | feature | COMPLETED (2026-07-13) | 010 | 6 · v0.1.0 |
| [CR-OA-015](CR-OA-015-orders-store.md) | `orders` delivery-lifecycle store | feature | COMPLETED | 005, 007 | 7 |
| [CR-OA-016](CR-OA-016-unified-skill-parity.md) | Complete unified skill (supersede legacy) | docs | COMPLETED | 012, 015 | 7 |
| [CR-OA-017](CR-OA-017-axi-conformance-audit.md) | AXI conformance audit + gap-closure (full verb surface) | maintenance | COMPLETED | 009, 010, 014 | 8 |
| [CR-OA-018](CR-OA-018-pluggable-backend-and-packaging.md) | Pluggable backend (SQLite default) + GPL-v3 `uv`/PyPI packaging | feature | COMPLETED | 016, 017 | 8 |
| [CR-OA-019](CR-OA-019-skill-mail-prerequisites.md) | Skill mail-access prerequisites (declared + orchestrated) | docs | SUPERSEDED (by 020) | 016 | 8 |
| [CR-OA-020](CR-OA-020-embedded-mail-client.md) | Embedded mail client in `voa` (Gmail/Fastmail/Yahoo + vault-first creds) | feature | COMPLETED (2026-07-27) | 017 | 9 |
| [CR-OA-021](CR-OA-021-skill-mail-verbs.md) | Skill revision — `Mailboxes & search` uses `voa mail-*` verbs | docs | COMPLETED (2026-07-27) | 016, 020 | 9 |
| [CR-OA-022](CR-OA-022-embedded-mail-sending.md) | Embedded mail *sending* in `voa` (draft-then-confirm, per-mailbox identity, store-linked) | feature | PENDING | 020 | 10 |
| [CR-OA-023](CR-OA-023-keyring-primary-os-aware-secret-setup.md) | Keyring-primary secret store + OS-aware `setup` (drop vault backends; keyring a base dep) | feature | PENDING | 020 | 10 |

## v0.1.0 milestone — Wave 6

CRs **011–014** constitute the **v0.1.0 release** (design contract:
[`../research/PRD-distribution-release.md`](../research/PRD-distribution-release.md)): packaging + rebrand →
**Vidushi OA**, the unified cross-harness skill, and the two approved AXI refinements (disposition-aware
`due-sweep`, aggregate tally). **Order:** 011 → 012 (the skill needs the package), with 013 + 014
(independent store refinements) runnable any time after their deps.

**✅ All four merged (2026-07-13) — v0.1.0 is cut and shipped.** The operational pre-tag steps are all
done: the release tag/version bump landed, and both parked data ops are resolved (the old
`office_assistant` backup DB was dropped post-v0.1.0 — see **Follow-up tasks** below — and the refreshed
snapshots were chezmoi-committed). Future releases are now guarded by a standing pre-`git flow release
finish` gate (`~/.claude/scripts/skill-release-gate.py`, declared in `.skill-release.toml`; see
[`../../scripts/README.md`](../../scripts/README.md)).

- **Pending decision — license (DN §6):** OSS-vs-private gates CI/CD *and* a public PyPI publish. v0.1.0 can
  ship **privately** (git-install) and go public later.
- **Beyond v0.1.0 (roadmap, not yet CRs):** reporting/export verb · 3rd mailbox (Yahoo — roll-our-own) ·
  attention TUI (needs a storyboard) · CI/CD (after OSS) · MCP wrapper (only if a non-CLI harness needs it).

## Wave 7 — unified-skill parity (post-v0.1.0)

CRs **015–016** make the unified `vidushi-oa` skill (CR-OA-012) a **complete drop-in** that formally
supersedes the seven legacy `~/.claude/skills/` role-skills + the `inbox-analyst` agent (pre-0.1.0
vestiges that live outside the repo). A gap review found the unified skill ~85% there — a correct
conceptual + safety + backend superset — but with two functional holes and a layer of operational
detail compressed out:

- **015** adds the missing **`orders`** store (the fulfilment state machine) so the purchase domain
  persists on the backend instead of a phantom placeholder, and revises PRD §3 to split fulfilment
  off `invoices` — design in [`../research/DN-purchases-persistence.md`](../research/DN-purchases-persistence.md).
- **016** corrects the invalid `cases.status` enum, wires the purchase domain to `orders`, gives
  `insurance` a first-class domain section, restores the operational specifics via
  `skills/vidushi-oa/references/`, and flips the roster (CLAUDE.md + README) to the unified skill.
  Its ACs **are** the coverage matrix.

**Order:** 015 → 016 (the skill's purchase domain needs the store). Both run on feature branches;
015 is a code CR (RED/GREEN/VERIFY), 016 is docs/skill (orchestrator-authored + grep/validate gates).

## Wave 8 — distribution readiness (portable, publishable engine)

CRs **017–018** turn the engine into a genuinely portable, publishable product. **017** audits the CLI
against the matured AXI spec + the `gh-axi` reference across the **full** verb surface and closes any
residual conformance gaps (envelope shape per verb, structured errors to stdout + exit codes, next-step
coverage) — a hardened interface to ship on. **018** makes persistence **pluggable** with an **embedded
SQLite default** (Mongo opt-in, `pymongo` optional) so the tool needs no server, licenses the repo
**GPL-3.0-or-later** (resolving the DN §6 gate), and wires the persistent **`uv tool install
vidushi-oa`** / PyPI distribution + CI/CD. Design in
[`../research/DN-persistence-mongodb.md`](../research/DN-persistence-mongodb.md) (2026-07-25) +
[`../research/DN-packaging-distribution.md`](../research/DN-packaging-distribution.md) (Decisions 6–7).

**019** makes the skill's **mail-access MCP prerequisites** explicit (declared + orchestrated, per
DN-packaging-distribution Decision 8): FastmailMCP + a Gmail provider are declared in `SKILL.md` (the
engine is mail-agnostic, so these are skill prereqs, not pip-deps), with a machine-readable MCP manifest
where a harness supports one and per-harness setup docs — honest that the claude.ai Gmail connector is
harness-specific and not installable.

**Order:** 017 → 018 (packaging ships on the audited, conformant CLI). §S6/§S4 note: the first PyPI
publish, the repo-public flip, and the GPL-license/CI packaging-deploy test are **release-branch** ops
during `git flow release` (guarded by the release gate), not inside 018. CR-OA-018 §S4 performs the **live
`vidushi_oa`→SQLite data migration** with field-level fidelity (in-record `actions[]`/logs/`documents[]`
preserved) + JSONL-snapshot rollback, leaving the old Mongo DB intact. **CR-OA-019 is SUPERSEDED** — see
Wave 9.

## Wave 9 — embedded mail access (tokens into the backend)

Reading mail is the last mechanical task still done by the LLM (via mail MCPs). Wave 9 **embeds** it in the
engine so "read my mail" becomes a pre-computed, TOON-shaped tool call — the framework principle applied to
mail. Design + primary-source research in
[`../research/DN-mail-access.md`](../research/DN-mail-access.md).

- **020** adds a **unified mail client** in `voa` over **Gmail / Fastmail / Yahoo** — IMAP common
  denominator (Gmail via `X-GM-RAW`, Yahoo plain, stdlib `imaplib`) + thin-HTTP **JMAP** for Fastmail; a
  **vault-first** secret resolver (1Password/Bitwarden primary, OS keyring fallback, `voa` holds only
  references); and AXI `mail-*` verbs that server-side-search, merge, de-dup, source-tag, and emit TOON.
  Net new deps ~0–2. This **supersedes CR-OA-019** (no MCP prerequisite once embedded).
- **021** repoints the skill's "Mailboxes & search" from MCP calls to `voa mail-*` (spec authored at
  wave-open, once 020's verb surface is final).

**Order:** 017 (conformant CLI) → 020 → 021. Independent of the 018 packaging track; both can proceed in
parallel after 017.

**Recommended order:** 001 → 002 → 003 → **006 → 004** → 005 → 007 → 009 → 008.
(2026-07-11: 006 pulled ahead of 004 — after the CRUD refactor the Mongo store is empty and the
tracking verbs still read JSONL; importing next repopulates Mongo so the store is functional
end-to-end. Both 006 and 004 depend only on the now-shipped 003.)

### Notes
- **CR-OA-009 pivoted (2026-07-12):** the MCP-server scope was **dropped** for **TOON output over the
  CLI** (the AXI stance) — lower per-task tokens, one pinned dependency instead of ~28, nothing to
  enable/reload. Rationale + the library-verification finding are in
  [`../research/DN-agent-interface-toon.md`](../research/DN-agent-interface-toon.md); the spec was renamed
  `CR-OA-009-mcp-interface.md → CR-OA-009-toon-output.md` and now depends on 007 (its ACs read the
  `subscriptions` store).
- **CR-OA-010 scheduled (2026-07-12):** AXI is **ten** principles (axi.md); CR-OA-009 delivers only #1
  (TOON). The store already meets #6 + #10. The remaining ergonomics — #2 minimal default fields, #3
  `--full` truncation, #4 pre-computed aggregates, #5 definitive empty states, #7 an ambient-context hook,
  #8 no-arg live data, #9 contextual next-command hints — are scheduled as **CR-OA-010** (user-reviewed;
  #4/#5/#7/#8/#9 explicitly approved).
- The already-built `store.py` v1 tracking verbs + the applied JSONL backfill (48 invoices
  COMPLETED, 19 warranties IN_PROGRESS, FNIRSI actions OPEN) are the **starting point** CR-OA-003
  / 004 port onto pymongo — not to be redone.
- `data/*.jsonl` stay as the `snapshot` target (chezmoi-versioned); they are NOT committed to the
  project repo (gitignored). Mongo data lives on the local instance only.

## Follow-up tasks
Small items (no design surface → tasks, not CRs) surfaced during execution:
- **Coverage source path** (filed 2026-07-11, from CR-OA-002 regression gate) — `python-crucible
  regression --coverage` runs `coverage run --source app`, but this project's code is in `scripts/`,
  so no coverage is collected (`No data was collected`). Point coverage at `scripts/` (a `.coveragerc`
  `[run]\nsource = scripts`, or a `--source` override in the gate). Tests still gate green; only the
  coverage metric is missing.
- **`data/schema.md` `cases.status` enum** (filed 2026-07-11, from CR-OA-002) — schema.md still shows
  the old lowercase `open|awaiting_support|…|closed`; the store now enforces the shared 6-value
  uppercase lifecycle `status`. **Resolved in CR-OA-008 §S2 (2026-07-12)** — schema.md `cases.status`
  now documents the 6-value uppercase lifecycle; `grep -c awaiting_support data/schema.md == 0`.
- **`store.py:70` PEP8 spacing** (filed 2026-07-11, from CR-OA-003 VERIFY) — missing blank-line pair
  before `def path(t):` after Cycle A removed `_CACHE`; cosmetic, fold into a lint pass.
- **Disposition-aware `due-sweep` action** (filed 2026-07-12, from CR-OA-007 live dry-run) — the
  `renewal-window` transition opens `cancel-before-charge` uniformly for every subscription, but a
  **KEEP** sub (Fastmail, Anthropic) reaching its renewal window wants a `renewal-confirm`/protect
  action, not a cancel prompt; only **TOMBSTONE/UNDECIDED** subs should get `cancel-before-charge`.
  Make the opened action disposition-aware (transition effect keyed on `disposition`, or a
  post-sweep pass). **Resolved in CR-OA-013 (2026-07-13)** — the `renewal-window` effect is now
  disposition-aware (`by_disposition {KEEP: renewal-confirm}`); a live `due-sweep` on the migrated KEEP
  subscriptions is safe to run.
- **Drop the old `office_assistant` Mongo DB** (filed 2026-07-12, from CR-OA-011 §S4) — the live data was
  migrated to `vidushi_oa` (118 records, count-parity + validator-clean verified) and the old
  `office_assistant` DB was retained as a backup pending confirmation. **Resolved 2026-07-13 (post-v0.1.0):**
  on explicit user confirmation the `office_assistant` DB (and a stray `office_assistant_cr009_probe` test
  DB) were dropped after re-verifying `vidushi_oa` = 118 + validator-clean; `vidushi_oa` is now the sole
  store. Snapshots were chezmoi-committed (local) beforehand.
