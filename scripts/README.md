# Vidushi OA — Scripts

Token-frugal helpers over the **active-backend** stores — SQLite by default (zero-config, no server),
MongoDB opt-in — (mirrored to `data/*.jsonl` by `snapshot`). **Skills
and agents MUST go through these instead of reading whole files into context** — query for exactly the
rows/fields needed.

## Installing the skill bundle

From the **repo root**, install the skill + engine together (full detail — incl. the published
`uv tool install vidushi-oa` path — in `../README.md` → **Install**):
```bash
npx skills add ./skills/vidushi-oa      # the vidushi-oa skill (flat-layout vercel/skills bundle)
uv tool install --editable .            # the voa engine
voa setup                               # provision the active backend (SQLite default)
```
The shape gate `agentskills validate skills/vidushi-oa` (see **Release gate** below) confirms the bundle.

## `voa` — data store CLI (pluggable backend: SQLite default / Mongo opt-in)

The console command is **`voa`** (`uv tool install vidushi-oa`, or in-repo `uv tool install --editable .`). The in-repo
`scripts/store.py` stays a path-compat shim (`python3 scripts/store.py <verb>` == `voa <verb>`).

Types: `contacts` · `invoices` · `warranties` · `cases` · `products` · `subscriptions` · `insurance` · `orders`
(schema in `../data/schema.md`).

```bash
# look up a vendor's verified support contact (only the fields you need)
voa query contacts --where vendor=LionCircuits --fields support_email,portal,rma_process

# open purchase documents missing a saved file copy
voa query invoices --where file=None --fields id,vendor,number,date,source.email_id

# add a record (id + updated auto-filled); --json is a JSON object matching the schema
voa add invoices --json '{"doc_type":"invoice","vendor":"...","date":"2026-..","amount":0,"acct":"personal","source":{"mailbox":"FM","email_id":"..."}}'

# fetch / patch / log / remove / count
voa get cases case_acme_1
voa update cases case_acme_1 --json '{"status":"IN_PROGRESS"}' --append-log "Sent RMA request"
voa rm invoices doc_x
voa stats invoices --by acct
```

Filters: `--where field=value` (exact), `--contains field=substr` (case-insensitive), and date-range
`--after field=YYYY-MM-DD` / `--before field=YYYY-MM-DD` (ISO date, **inclusive** on both ends; null/missing
dates are excluded). All are repeatable and AND-combined, and accept **dotted paths** (`source.email_id`,
`registration.done`, `last_contact.date`). `--fields a,b,c` projects; `--sort`, `--limit`.
Output is **TOON by default** — token-efficient (pass `--json` or set `VIDUSHI_FORMAT=json` for JSON); warnings go to stderr.

**Lifecycle + admin verbs:** `set-status` / `action-add` / `action-resolve` / `doc-add` drive the shared
`status` + `actions[]`; `event <type> <id> <event>` fires a mapped `transitions.py` transition; `attention`
lists rows needing action; `warranty-sweep` / `due-sweep` / `delivery-sweep` expire / renew / chase in bulk.
`setup` provisions the **active backend** then `init`s it (tables/collections + unique `id` index + schema
validators); `validate` reports violations; `import` / `snapshot` move data between `data/*.jsonl` and the
active backend; `doctor` is a diagnostic health read (engine version, store + secret backend, per-account
resolution — absorbs `setup --check`).

**Embedded mail client (read-only):** `mail-search '<query>' [--accounts a,b]` searches the configured
accounts server-side and merges + de-dupes by `Message-ID` (fail-soft per account); `mail-accounts` lists
them; `mail-get --account <name> --uid <uid>` fetches one message; `mail-auth --provider <p> --address <a>`
registers a credential *reference* (never the secret) with `--auth-mode password|xoauth2`.

**Backend selection:** `VIDUSHI_BACKEND` picks **`sqlite`** (default, at `$XDG_DATA_HOME/vidushi-oa/oa.db`,
override `VIDUSHI_SQLITE_PATH`) or **`mongo`** (needs the `[mongo]` extra) on `127.0.0.1:27017` db
`vidushi_oa`, overridable via `VIDUSHI_MONGO_URI` / `VIDUSHI_MONGO_DB` (`VIDUSHI_DATA_DIR` relocates the
snapshot/import dir).

## Release gate (run BEFORE `git flow release finish`)

The pre-finish gate validates the **shipped artifacts**, not the source tree, so it catches what the
unit suite can't. It's the **generic, cross-project** ecosystem tool
`~/.claude/scripts/skill-release-gate.py`, **vendored here as `scripts/skill-release-gate.py`** (so CI
runners, which have no `~/.claude`, can run it — keep the copy in sync); this repo just declares its
specifics in a standalone `.skill-release.toml`. Run it on the release branch — a non-zero exit means
**do not finish the release**.

```bash
env -u VIDUSHI_BACKEND .venv/bin/python scripts/skill-release-gate.py --project-dir .
```

The gate is **one step of the mandatory release checklist** — the full ordered process (no-mistakes +
reconcile → TestPyPI dry-run → gate + full test suite → irreversible-publish confirm) is in
[`../AGENTS.md`](../AGENTS.md) → **Release process**, with the CI/release model in
[`../docs/memory/cicd-release-convention.md`](../docs/memory/cicd-release-convention.md).

Phases (each auto-skips when its config is absent):
1. **Skill-bundle conformance** — wraps the official **`agentskills validate`** (`pip install
   skills-ref`; auto-provisioned in a throwaway venv if absent) over every `skills/<name>/SKILL.md`,
   validating against the Agent Skills standard ([agentskills.io](https://agentskills.io/specification))
   + the Vercel `npx skills` flat layout.
2. **Engine build + packaging** — `python -m build --wheel`; asserts the `voa` entry point, that the
   8 schema validators are bundled, and no stray `oa` entry point.
3. **Clean-install smoke** — installs the wheel into a fresh venv; confirms `voa` runs and imports
   from site-packages (neutral cwd).
4. **Lifecycle + AXI conformance** — declarative `[[check]]` steps (in `.skill-release.toml`) run
   against the installed `voa` over a **throwaway** store (`setup → add → due-sweep → attention → validate`),
   plus the 10 [axi.md](https://axi.md) principles + the decision-B `--json` contract.

The checks must exercise the **shipped SQLite default**, so `.skill-release.toml` `[env]` pins
`VIDUSHI_BACKEND = "sqlite"` + a throwaway `VIDUSHI_SQLITE_PATH = "$TMP/oa.db"`. Without the pin an
ambient `VIDUSHI_BACKEND=mongo` (this machine's local setting) leaks into the inherited runner env and
the gate would validate Mongo instead; without the path override sqlite would fall back to the
operator's real `$XDG_DATA_HOME/vidushi-oa/oa.db`. The `env -u VIDUSHI_BACKEND` prefix above is
belt-and-braces (and the shell here is **fish**, where a bare `VAR=… cmd` prefix fails — use `env`).

Nothing touches the live store (throwaway sqlite path + venv, plus the throwaway Mongo DB `[teardown]`
still drops — all cleaned up on exit).
Self-test the gate with `env SKILLGATE_SOON=2999-01-01 .venv/bin/python scripts/skill-release-gate.py
--project-dir .` — the out-of-window renewal makes `due-sweep` skip, so the gate must FAIL (exit 1).

**Authoring a new skill** (the init counterpart to this gate): use the **skill-creator** skill
(anthropics/skills) to scaffold it — conformant frontmatter, progressive-disclosure layout
(`references/`+`scripts/`+`assets/`), and `python -m scripts.package_skill <dir>` to bundle a
distributable `.skill` file. skill-creator *authors*; `agentskills validate` (this gate) *checks*.

## Conventions
- One concern per call; let the script do the filtering — don't pull the whole store back.
- `acct` is `personal` or `business` (business = bought on `antojk@anthilllabs.in`, usually GST).
- Saved document copies live under `../documents/<acct>/<vendor>/`; the JSONL row's `file` points to them.
- Extend with more scripts here (e.g. an expense/tax summarizer) as needs grow — keep them JSON-out.
