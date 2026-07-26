# AGENTS.md — Vidushi OA (parent-project agent guide)

The cross-harness guide for AI agents working in **this repository**. Portable per the AGENTS.md
convention; `CLAUDE.md` is a **symlink** to this file so Claude Code reads the same guide. Any harness
that reads `AGENTS.md` (Codex, Cursor, OpenCode, …) gets identical guidance.

## Project memory

Durable, project-specific operating notes live in **[`docs/memory/`](docs/memory/)** (git-tracked,
in-repo — never a harness's private store), indexed in [`docs/memory/README.md`](docs/memory/README.md).
Read them for non-obvious project context (CI/release convention, packaging follow-ups). These are about
*building* this project — **not** part of the shipped `skills/vidushi-oa/` bundle.

## What this project is

This project is **Vidushi OA** — a **functional personal office assistant, not just code** — an evolving **set of role-based capabilities** (Claude *skills* + an *agent*, defined under "Vidushi OA toolkit — roles" below) that read the user's mail and run the post-purchase / personal-admin lifecycle, operating over the **shared data + document store kept in this repo**.

Think of it as *a system of roles plus its persistent memory*: the one executable is the **`voa`** CLI (the installable `vidushi-oa` package; the in-repo `scripts/store.py` remains a path-compat shim), backed by a **local store — SQLite by default (zero-config, no server), MongoDB opt-in** (CR-OA-018); the actual behaviour lives in the roles. **The roster of roles is the primary artifact of this project**, and this repo is the durable state they all share. There is no long-running server; a `pytest` suite guards the CLI. (The repo/folder name stays `office_assistant`; the *product* is Vidushi OA.)

## Cardinal rules

1. **Access the stores ONLY through the `voa` CLI** (the in-repo `scripts/store.py` is a path-compat shim to it). The store is **SQLite by default** (`$XDG_DATA_HOME/vidushi-oa/oa.db`; MongoDB opt-in via `VIDUSHI_BACKEND=mongo`); `data/*.jsonl` are `snapshot` outputs (chezmoi-versioned), not the source of truth. Query the CLI for exactly the rows/fields you need (token-frugal). Going around it — hand-editing the JSONL snapshots or hitting the DB directly — bypasses id-generation, dedupe, the schema validators, and the state-machine transitions.
2. **Use the purpose-built skills for their tasks — don't improvise.** When a request matches a skill's domain (below), invoke that skill *first* and follow its steps; reach for the browser/web only as a skill's own documented fallback. (This is a standing user rule; improvising has caused real misses.)

## Commands (the `voa` CLI — pluggable backend: SQLite default / Mongo opt-in)

Types: `contacts` · `invoices` · `warranties` · `cases` · `products` · `subscriptions` · `insurance`. Full field schemas in `data/schema.md`.

```bash
# read — filter + project only what you need (dotted paths supported: source.email_id, registration.done)
voa query <type> [--where f=v] [--contains f=sub] [--after f=YYYY-MM-DD] [--before f=YYYY-MM-DD] [--fields a,b.c] [--sort f] [--limit N]   # --after/--before = inclusive ISO date range
voa get <type> <id> [--fields ...] [--expand <fk,fk>]   # --expand resolves FKs inline as <fk>_obj

# write — id + updated auto-filled; --json takes ONE object OR an array (bulk); add de-dupes by id
voa add <type> --json '{...}'        # or '[{...},{...}]'
voa update <type> <id> --json '{...}' [--append-log "note"]   # shallow-merge; --append-log is for cases
voa rm <type> <id>
voa stats <type> [--by field]

# lifecycle state — shared status + per-domain action set (see data/schema.md)
voa set-status <type> <id> <STATUS>
voa action-add <type> <id> --json '{"action":"...","owner":"user"}'
voa action-resolve <type> <id> <action>
voa doc-add <type> <id> --json '{...}'
voa event <type> <id> <event>        # fire a mapped state transition (transitions.py)
voa attention [<type>]               # rows with an OPEN action or a status needing attention
voa warranty-sweep [--dry-run]       # expire past-term warranties (+ open renew-or-extend)
voa due-sweep [--dry-run]            # flag subscriptions/insurance inside the renewal window

# admin — active-backend provisioning, schema validation, migration + versioning
voa setup [--check]                  # provision the active backend (SQLite default / Mongo), then init (--check diagnoses only)
voa init                             # create tables/collections + unique id index + schema validators
voa validate [<type>]                # list docs that violate the validator ([] = clean)
voa import [<type>]                  # data/*.jsonl -> active backend (idempotent upsert by id)
voa snapshot [<type>]                # active backend -> data/*.jsonl (chezmoi-versioned; the migration bridge)
```

> The console command is **`voa`** (from `uv tool install vidushi-oa`, or in-repo `uv tool install --editable .`). The
> in-repo **`scripts/store.py`** stays a thin path-compat shim to the same CLI (`python3 scripts/store.py <verb>`).

**Backend selection (`VIDUSHI_BACKEND`):** **`sqlite`** (default) at `$XDG_DATA_HOME/vidushi-oa/oa.db` (override `VIDUSHI_SQLITE_PATH`); **`mongo`** (opt-in — needs the `[mongo]` extra) on `127.0.0.1:27017` db `vidushi_oa`, via `VIDUSHI_MONGO_URI` / `VIDUSHI_MONGO_DB`. `VIDUSHI_DATA_DIR` sets the snapshot/import directory (the test suite uses it for isolation). A fresh `uv tool install vidushi-oa` → `voa setup` provisions the active backend. The CLI builds a **neutral query model** each backend compiles to its own native query (no dialect-translation layer).

**Ambient-context hook (AXI #7):** `.claude/settings.json` registers a Claude Code **SessionStart** hook that runs the bare CLI (via the `scripts/store.py` shim, no verb — read-only) so the **attention** worklist (rows with an OPEN action or a status needing attention) is surfaced automatically at the start of every session, before the agent acts. The hook prefers the repo `.venv/bin/python` when it exists (the editable in-repo install the system `python3` can't see on PEP 668 Pythons) and otherwise falls back to system `python3` — so a missing `.venv` no longer breaks session start.

IDs are auto-generated from anchor fields (`ven_<vendor>`, `doc_<vendor>_<number|date>`, `war_<vendor>_<product>`, `case_<vendor>`, `prod_<manufacturer>_<model>`, `sub_<provider>`, `ins_<insurer>_<policy_no>`). Output is **TOON by default** (token-efficient — `query` is enveloped `{count, results, next}` with minimal default fields + `…(+N chars)` truncation; `--full` shows all fields untruncated; `--json`/`VIDUSHI_FORMAT=json` gives a clean full JSON array). Bare `voa` (no verb) prints the `attention` worklist. Warnings to stderr. The shell here is **fish** — `VAR=...` assignment fails; use full paths or `set`.

## Architecture (the big picture)

**Seven stores** (SQLite tables by default, or Mongo collections; mirrored to `data/*.jsonl` by `snapshot`) **form a small relational model**, joined by foreign keys and resolved with `voa … --expand`:

```
invoice (proof of purchase)  ──invoice_id──┐
 └─ documents/<acct>/<vendor>/*.pdf (file)  │
warranty (coverage + expiry) ──warranty_id──┼──> product (manual/specs/official links)
 └─ contact (verified support) <──contact_id─┘        └─ contact_id ──> contacts
case (claim/RMA) ── invoice_id / warranty_id / product_id / contact_id ──> all of the above
```

- **FK fields** (`contact_id`, `invoice_id`, `warranty_id`, `product_id`) → see `FK_MAP` in the `vidushi_oa` CLI. One `--expand` call does a rich join (e.g. product → its warranty expiry → its invoice PDF → its support email).
- **`acct` splits personal vs business** everywhere (`business` = bought on `antojk@anthilllabs.in`, usually with a GSTIN). Mirrored in `documents/personal/` vs `documents/business/`.
- **`documents/<acct>/<vendor>/`** holds saved PDF copies (named `YYYY-MM-DD_<vendor>_<doctype>_<number>.pdf`); the invoice row's `file` points to it. The store row always pins the originating mail (`source`) even when no copy is saved.
- **Products are keyed on the actual MANUFACTURER**, not the reseller (`bought_from`); their `links` must be manufacturer-official.
- **`subscriptions` + `insurance`** are the recurring domains (billing/coverage that renews): `insurance` links a `product_id` (e.g. a vehicle's motor policy), and an `invoice` may carry a `subscription_id`. They ride the `DUE` status via `due-sweep`.
- **Every row carries the shared lifecycle** — a `status` (NEW/UNKNOWN/IN_PROGRESS/COMPLETED, +EXPIRED for warranties, +DUE for recurring), a domain-specific `actions[]` set (each OPEN→RESOLVED), and `documents[]`. Transitions are locked into `transitions.py` and fired via `event`/the sweeps — see `data/schema.md`.

**Data sources:** the skills search **two mailboxes** — Fastmail (FastmailMCP) and Gmail `antojk@gmail.com` (claude.ai connector) — and write findings here.

## Vidushi OA toolkit — roles

**The canonical role is the single unified skill `skills/vidushi-oa/` (in-repo)** — a portable,
harness-agnostic superset that **supersedes** the seven legacy standalone skills in `~/.claude/skills/`
(`mail-tracking-core`, `subscription-watch`, `purchase-tracker`, `invoice-tracker`, `warranty-tracker`,
`product-catalogue`, `support-case-manager`) **and** the `inbox-analyst` agent (folded in as the skill's
read-only **deep-sweep mode**). Load it for any mail/admin task; its operational detail lives in
`skills/vidushi-oa/references/`, and the domains below are its sections.

**Coverage / fidelity — every legacy capability maps into the unified skill:**

| Legacy skill / agent | → Unified skill location |
|---|---|
| `mail-tracking-core` | SKILL.md "Mailboxes & search" + "Safety contract" + `references/search-recipes.md`, `references/calendar-reminders.md` |
| `subscription-watch` | Domain "Subscription" + `references/subscription-taxonomy.md` |
| `purchase-tracker` | Domain "Purchase" (store `orders`) + `references/carriers-and-customs.md` |
| `invoice-tracker` | Domain "Invoice" + `references/report-templates.md` (retrieval tiers, expense/tax) |
| `warranty-tracker` | Domain "Warranty" |
| `product-catalogue` | Domain "Product" |
| `support-case-manager` | Domain "Support" (store `cases`, shared lifecycle) |
| `inbox-analyst` (agent) | "Deep-sweep mode (read-only)" |

**Replacement path (a one-time swap; pruning the legacy files is the user's, outside this repo):**
1. **Install** the bundle (see `README.md` / `scripts/README.md` install section): `npx skills add ./skills/vidushi-oa` for the skill + the engine (`uv tool install vidushi-oa`, or `--editable .` in-repo, then `voa setup`).
2. **Verify** it: `agentskills validate skills/vidushi-oa` exits 0 (or run the release gate `~/.claude/scripts/skill-release-gate.py`).
3. **Remove** the seven legacy `~/.claude/skills/` skills + the `inbox-analyst` agent — the unified skill now covers them all.

**Domains of the unified skill** (each writes through `voa`; load `references/*` for specifics):
- **Subscription** — recurring billing: classify by type, surface actions + deadlines up front, hold a per-item **KEEP/TOMBSTONE disposition** (protect KEEP, warn-to-cancel TOMBSTONE before a charge). Store `subscriptions`.
- **Purchase** — order → delivery lifecycle on the **`orders`** store; leads with **not-yet-delivered**; **international/customs** (duty/IGST/KYC/clearance) as OPEN actions; STUCK via `voa delivery-sweep`.
- **Invoice** — purchase **documents** (PO/invoice/receipt) → proof-of-purchase; saves PDF copies to `documents/<acct>/<vendor>/`; splits **personal vs business/GST**. Store `invoices`.
- **Warranty** — coverage / term / **expiry** + registration; links `invoice_id`; expiry reminders. Store `warranties`. **Never invents terms.**
- **Product** — manufacturer-**OFFICIAL** references per owned product + key specs. Store `products`, keyed on **manufacturer**.
- **Support** — stateful **claims / RMA / returns / service** cases on the **shared lifecycle**; **DRAFTS** mail to the verified support contact (**draft-then-confirm, never auto-send**), cites invoice + warranty. Store `cases`.
- **Insurance** — policies + **regulatory renewals** (RC re-registration / fitness) riding `DUE` via `voa due-sweep`. Store `insurance`.
- **Deep-sweep mode** (read-only) — heavy cross-mailbox triage returning structured findings; mutates nothing (the folded-in `inbox-analyst`).

**Supporting capabilities — a documented fallback, not the default**
- **`claude-in-chrome`** (browser) — login-gated data (Amazon/portal invoices, Dell service tags, carrier tracking): the user logs in; the agent navigates/downloads; **never enters credentials**.
- **`read-the-damn-docs`** / `WebSearch` + `WebFetch` — confirm official manufacturer/third-party terms from primary sources instead of assuming.

**Orchestration model:** interactive decisions (dispositions, reminders, drafting/sending, deletions) run in the main thread; a big independent read pass uses the deep-sweep mode, then the main thread acts on its findings.

## Conventions that aren't obvious from the code

- **Verified contacts only** in `contacts`; outbound support mail is **draft-then-confirm**, sent only to a verified address (never one scraped from an email).
- **Never invent warranty terms** — record `term_months: null` + a note when unstated; confirm from the manufacturer's official policy (via `product-catalogue`).
- **Login-gated data** (Amazon/portal invoices, Dell service tags): the user logs in via the Chrome extension; the agent navigates/downloads — never enters their credentials.
- Convert relative dates to **absolute** before storing.
- **Extending:** add a new store by editing `STORES`/`PREFIX` (and `FK_MAP` if referenced) in the `vidushi_oa` package, adding a `vidushi_oa/schema/<type>.schema.json` validator (+ a `transitions.py` map if it has a lifecycle), then documenting fields in `data/schema.md` and running `voa setup`. New helper code stays JSON-out.
