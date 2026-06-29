# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

This project is a **functional personal "office assistant," not just code** — an evolving **set of role-based capabilities** (Claude *skills* + an *agent*, defined under "Office-assistant toolkit — roles" below) that read the user's mail and run the post-purchase / personal-admin lifecycle, operating over the **shared data + document store kept in this repo**.

Think of it as *a system of roles plus its persistent memory*: the only executable is the JSONL data CLI (`scripts/store.py`); the actual behaviour lives in the roles. **The roster of roles is the primary artifact of this project**, and this repo is the durable state they all share. There is no build step, server, or test suite.

## Cardinal rules

1. **Access the JSONL stores ONLY through `scripts/store.py`.** Never read or hand-parse the `data/*.jsonl` files into context — query the script for exactly the rows/fields you need (token-frugal; writes are atomic). Editing raw JSONL bypasses id-generation, dedupe, and atomic writes.
2. **Use the purpose-built skills for their tasks — don't improvise.** When a request matches a skill's domain (below), invoke that skill *first* and follow its steps; reach for the browser/web only as a skill's own documented fallback. (This is a standing user rule; improvising has caused real misses.)

## Commands (the data CLI — `python3`, stdlib only)

Types: `contacts` · `invoices` · `warranties` · `cases` · `products`. Full field schemas in `data/schema.md`.

```bash
# read — filter + project only what you need (dotted paths supported: source.email_id, registration.done)
python3 scripts/store.py query <type> [--where f=v] [--contains f=sub] [--after f=YYYY-MM-DD] [--before f=YYYY-MM-DD] [--fields a,b.c] [--sort f] [--limit N]   # --after/--before = inclusive ISO date range
python3 scripts/store.py get <type> <id> [--fields ...] [--expand <fk,fk>]   # --expand resolves FKs inline as <fk>_obj

# write — id + updated auto-filled; --json takes ONE object OR an array (bulk); add de-dupes by id
python3 scripts/store.py add <type> --json '{...}'        # or '[{...},{...}]'
python3 scripts/store.py update <type> <id> --json '{...}' [--append-log "note"]   # shallow-merge; --append-log is for cases
python3 scripts/store.py rm <type> <id>
python3 scripts/store.py stats <type> [--by field]
```

IDs are auto-generated from anchor fields (`ven_<vendor>`, `doc_<vendor>_<number|date>`, `war_<vendor>_<product>`, `case_<vendor>`, `prod_<manufacturer>_<model>`). Output is compact JSON on stdout; warnings to stderr. The shell here is **fish** — `VAR=...` assignment fails; use full paths or `set`.

## Architecture (the big picture)

**Five JSONL stores in `data/` form a small relational model**, joined by foreign keys and resolved with `store.py … --expand`:

```
invoice (proof of purchase)  ──invoice_id──┐
 └─ documents/<acct>/<vendor>/*.pdf (file)  │
warranty (coverage + expiry) ──warranty_id──┼──> product (manual/specs/official links)
 └─ contact (verified support) <──contact_id─┘        └─ contact_id ──> contacts
case (claim/RMA) ── invoice_id / warranty_id / product_id / contact_id ──> all of the above
```

- **FK fields** (`contact_id`, `invoice_id`, `warranty_id`, `product_id`) → see `FK_MAP` in `store.py`. One `--expand` call does a rich join (e.g. product → its warranty expiry → its invoice PDF → its support email).
- **`acct` splits personal vs business** everywhere (`business` = bought on `antojk@anthilllabs.in`, usually with a GSTIN). Mirrored in `documents/personal/` vs `documents/business/`.
- **`documents/<acct>/<vendor>/`** holds saved PDF copies (named `YYYY-MM-DD_<vendor>_<doctype>_<number>.pdf`); the invoice row's `file` points to it. The store row always pins the originating mail (`source`) even when no copy is saved.
- **Products are keyed on the actual MANUFACTURER**, not the reseller (`bought_from`); their `links` must be manufacturer-official.

**Data sources:** the skills search **two mailboxes** — Fastmail (FastmailMCP) and Gmail `antojk@gmail.com` (claude.ai connector) — and write findings here.

## Office-assistant toolkit — roles (skills in `~/.claude/skills/`, agents in `~/.claude/agents/`)

Invoke the skill whose **role** matches the request, and load `mail-tracking-core` alongside it. Quick map:
subscriptions→`subscription-watch` · deliveries/customs→`purchase-tracker` · invoices/receipts/POs→`invoice-tracker` · warranty/expiry→`warranty-tracker` · claims/RMA/support-mail→`support-case-manager` · manuals/specs/official-warranty→`product-catalogue`.

**Foundation (load with any mail task)**
- **`mail-tracking-core`** — shared engine: dual-mailbox (Fastmail via FastmailMCP + Gmail `antojk@gmail.com` via the claude.ai connector) search & merge with `[FM]`/`[GM]` source tagging, the phishing/customs **safety contract**, this JSONL data store (`store.py`), and calendar-reminder creation. Not run alone.

**Interactive trackers — run in the main thread, the user steers them**
- **`subscription-watch`** — recurring billing/subscriptions: classify by type, surface actions + deadlines up front, hold a per-item **KEEP/TOMBSTONE disposition** (flips advice: protect KEEP, warn-to-cancel TOMBSTONE before a charge).
- **`purchase-tracker`** — order → delivery lifecycle; leads with **not-yet-delivered** orders; first-class **international/customs** handling (duty/IGST/KYC/clearance), surfaced even when Customs/India Post emails the user directly.
- **`invoice-tracker`** — purchase **documents** (PO/invoice/receipt) → proof-of-purchase backbone; saves PDF copies to `documents/<acct>/<vendor>/`; splits **personal vs business/GST**. Store type `invoices`.
- **`warranty-tracker`** — coverage / term / **expiry** + registration; links `invoice_id`; proposes expiry calendar reminders. Store type `warranties`. **Never invents terms.**
- **`product-catalogue`** — manufacturer-**OFFICIAL** references per owned product (product page, manual, datasheet, support, drivers/firmware, official warranty policy) + key specs; WebFetch to answer "how do I / specs / driver". Store type `products`, keyed on **manufacturer** (not reseller).
- **`support-case-manager`** — stateful **claims / RMA / returns / service** cases; **DRAFTS** mail to the verified support contact (**draft-then-confirm, never auto-send**), cites invoice + warranty, logs each exchange. Store type `cases`.

**Agent — delegated, read-only**
- **`inbox-analyst`** (subagent) — heavy autonomous sweep across **both** mailboxes for a full pass (subscriptions / purchases / customs / invoices / warranties / general triage). Returns **structured findings + recommended actions** and **mutates nothing** — the main thread executes side effects (persist via `store.py`, create reminders, send mail). Dispatch it when a comprehensive scan would otherwise flood the conversation.

**Supporting capabilities — a skill's documented fallback, not the default**
- **`claude-in-chrome`** (browser) — drive the user's logged-in browser for **login-gated data** (Amazon/portal invoices, Dell service tags, carrier tracking). The user logs in; the agent navigates/downloads; **never enters credentials**.
- **`read-the-damn-docs`** / `WebSearch` + `WebFetch` — confirm official manufacturer/third-party terms (e.g. a warranty policy) from primary sources instead of assuming.

**Orchestration model:** interactive decisions (dispositions, reminders, drafting/sending, deletions) → run the trackers in the main thread; a big independent read pass → dispatch `inbox-analyst`, then act on its findings.

## Conventions that aren't obvious from the code

- **Verified contacts only** in `contacts`; outbound support mail is **draft-then-confirm**, sent only to a verified address (never one scraped from an email).
- **Never invent warranty terms** — record `term_months: null` + a note when unstated; confirm from the manufacturer's official policy (via `product-catalogue`).
- **Login-gated data** (Amazon/portal invoices, Dell service tags): the user logs in via the Chrome extension; the agent navigates/downloads — never enters their credentials.
- Convert relative dates to **absolute** before storing.
- **Extending:** add a new store by editing `STORES`/`PREFIX` (and `FK_MAP` if it's referenced) in `store.py` and documenting fields in `data/schema.md`. New helper scripts stay stdlib + JSON-out.
