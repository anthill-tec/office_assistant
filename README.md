# Vidushi OA

Engine `uv tool install vidushi-oa` (console `voa`) + the portable `vidushi-oa` skill. Install the skill with `npx skills add anthill-tec/office_assistant/skills/vidushi-oa`.

**Vidushi OA** is a personal office assistant that reads your mail (Fastmail + Gmail) and runs the
everyday admin lifecycle around the things you buy and pay for — subscriptions, purchases & deliveries,
invoices, warranties, support cases, and product references.

It is **a set of defined roles, not just code.** Each role is a Claude *skill* (or *agent*) with a
clear job; they all read and write one shared **data + document store** that lives in this folder.
The only program here is the **`voa`** CLI (the installable `vidushi-oa` package) — the behaviour lives
in the roles. Get started: `uv tool install vidushi-oa` (or, in-repo, `uv tool install --editable .`), then `voa setup`.

---

## The roles

> These roles are now unified into the single portable skill **`skills/vidushi-oa/`** (see **Install**
> below); the descriptions here map 1:1 to its domains, and the legacy standalone skills + the
> `inbox-analyst` agent are **superseded** by it.

### Foundation
| Role | Job |
|---|---|
| **mail-tracking-core** | Shared engine every mail task loads: searches **both** mailboxes (Fastmail + Gmail) and merges results, enforces the phishing/customs safety rules, owns the data store, and creates calendar reminders. |

### Trackers (interactive — you steer them)
| Role | Job |
|---|---|
| **subscription-watch** | Tracks recurring bills/subscriptions, leads with what needs action + deadlines, and remembers your **keep vs cancel ("tombstone")** decision per service. |
| **purchase-tracker** | Tracks orders through to delivery — what hasn't arrived yet first — including **international customs** (duty / KYC / clearance), even when Customs contacts you directly. |
| **invoice-tracker** | Captures purchase documents (PO / invoice / receipt) as your proof-of-purchase, saves PDF copies, and keeps **personal vs business (GST)** separate. |
| **warranty-tracker** | Records coverage and **expiry** per product, links the proof-of-purchase, and sets reminders before warranties lapse. Never guesses unknown terms. |
| **product-catalogue** | Keeps each owned product's **manufacturer-official** links — manual, datasheet, support, drivers, warranty policy — and specs, so "where's the manual / what are the specs" is one lookup. |
| **support-case-manager** | Runs warranty claims / RMAs / service requests as tracked cases, and **drafts** emails to the verified support contact (you review and send — it never sends on its own). |

### Agent (delegated, read-only)
| Role | Job |
|---|---|
| **inbox-analyst** | Does a heavy, full sweep across both inboxes and returns structured findings + suggested actions. It only reads — it changes nothing; the main assistant carries out anything that needs doing. |

### Fallbacks
- **Browser (Claude-in-Chrome)** — for anything behind a login (Amazon/portal invoices, Dell service tags, carrier tracking): **you log in**, the assistant reads/downloads. It never enters your credentials.
- **Web search/fetch** — to confirm official manufacturer/vendor terms (e.g. a warranty policy) from the source.

---

## Install (skill + engine)

Vidushi OA ships as two pieces: the **skill** (`skills/vidushi-oa/` — a portable vercel/skills
flat-layout bundle with its `references/`) and the **engine** (the `vidushi-oa` pip package that
provides the `voa` CLI). Install both, then `voa setup`.

**Local / dev (works today):**
```bash
npx skills add ./skills/vidushi-oa                    # add the skill from this repo
uv tool install --editable .                          # the engine (voa), editable/in-repo
voa setup                                             # provision the active backend (SQLite by default)
agentskills validate skills/vidushi-oa                # confirm the bundle shape (exits 0)
```

**Public (one-liner — the normal path):**
```bash
uv tool install vidushi-oa
npx skills add anthill-tec/office_assistant/skills/vidushi-oa
voa setup
```
> The engine is published on [PyPI](https://pypi.org/project/vidushi-oa/) and the repo
> (`anthill-tec/office_assistant`) is public, so this path works today. Releases are cut from `main`
> via git-flow and publish automatically — see [`AGENTS.md`](AGENTS.md) → **Release process**.

> **Backend.** SQLite is the default backend — zero-config, no server, nothing to provision. MongoDB is
> opt-in: install the extra with `uv tool install "vidushi-oa[mongo]"` and select it with
> `VIDUSHI_BACKEND=mongo`.

The unified `skills/vidushi-oa/` skill **supersedes** the seven legacy `~/.claude/skills/` skills +
the `inbox-analyst` agent; after installing and verifying it, remove those legacy files (see
`CLAUDE.md` → "Vidushi OA toolkit — roles" for the coverage matrix + replacement steps).

---

## The data store

Everything the roles learn is kept in the **active backend** — SQLite by default (db `vidushi_oa`,
zero-config), or MongoDB opt-in (`VIDUSHI_BACKEND=mongo`) — and accessed through the **`voa`** CLI,
never directly. `voa snapshot` mirrors the store to `data/*.jsonl` for versioning.

Eight stores form a small **relational model**, joined by foreign keys:

```
invoice (proof of purchase) → warranty (coverage/expiry) → product (manual/specs)
        ↘ saved PDF in documents/        ↘ contact (verified support) ↙
                         case (claim / RMA) links them all
```

- **contacts** — verified vendor/manufacturer support directory
- **invoices** — purchase documents (PO / invoice / receipt), with the originating email pinned and any saved PDF
- **orders** — the purchase **fulfilment / delivery** lifecycle (ordered → shipped → delivered, including international customs), linked to its invoice
- **warranties** — coverage, term, expiry, registration
- **products** — owned products keyed on the **manufacturer**, with official reference links + specs
- **cases** — support / claim / RMA / service cases
- **subscriptions** — recurring bills / SaaS / memberships, with your keep-vs-cancel disposition
- **insurance** — policies + regulatory renewals (e.g. a vehicle's motor policy + RC re-registration), linked to the insured product

Saved document copies live in `documents/personal/` and `documents/business/` (by vendor); each
invoice record points to its PDF. Renewal/warranty/customs reminders go to the calendar.

### Using the CLI
```bash
# the vendor's verified support email + returns process
voa query contacts --where vendor=FNIRSI --fields support_email,rma_process

# a product with its warranty, invoice PDF, and support contact in one call (FK join)
voa get products prod_fnirsi_2c53t --expand warranty_id,invoice_id,contact_id

# what's covered, soonest expiry first
voa query warranties --fields product,expiry --sort expiry
```
The in-repo `scripts/store.py` remains a path-compat shim (`python3 scripts/store.py <verb>`).
Full field reference: `data/schema.md`. CLI details: `scripts/README.md`.

> **Local `.venv` dependency.** The primary install path is `uv tool install vidushi-oa` (persistent,
> isolated). On externally-managed Pythons (Arch/PEP 668) an in-repo editable install can instead live in
> a repo `.venv` (`python -m venv .venv && .venv/bin/pip install -e .`). The Claude Code
> **SessionStart** hook in `.claude/settings.json` prefers `"$CLAUDE_PROJECT_DIR/.venv/bin/python"` when
> that venv exists (the system `python3` can't see the editable install) and otherwise falls back to
> system `python3` — so a `uv tool install vidushi-oa` on PATH still works and a missing `.venv` no longer
> breaks session start.

---

## Layout
```
vidushi_oa/  the voa CLI package (+ schema)    data/      JSONL snapshots + schema.md
scripts/     store.py path-compat shim + README   documents/ saved PDFs (personal | business)
pyproject.toml  packaging (vidushi-oa)          CLAUDE.md  guidance for Claude Code
```

This is a **living system** — roles and stores are added as needs grow (the rule of thumb: a new role
is a new skill, a new kind of record is a new store + a line in `data/schema.md`).
