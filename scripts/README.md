# Vidushi OA — Scripts

Token-frugal helpers over the **MongoDB** stores (mirrored to `data/*.jsonl` by `snapshot`). **Skills
and agents MUST go through these instead of reading whole files into context** — query for exactly the
rows/fields needed.

## `voa` — data store CLI (`pymongo`, MongoDB-backed)

The console command is **`voa`** (`pip install vidushi-oa`, or `pip install -e .` in-repo). The in-repo
`scripts/store.py` stays a path-compat shim (`python3 scripts/store.py <verb>` == `voa <verb>`).

Types: `contacts` · `invoices` · `warranties` · `cases` · `products` · `subscriptions` · `insurance`
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
lists rows needing action; `warranty-sweep` / `due-sweep` expire/renew in bulk. `setup` verifies/provisions
the local MongoDB then `init`s it (collections + unique `id` index + `$jsonSchema` validators); `validate`
reports violations; `import` / `snapshot` move data between `data/*.jsonl` and Mongo. Connection:
`127.0.0.1:27017` db `vidushi_oa`, overridable via `VIDUSHI_MONGO_URI` / `VIDUSHI_MONGO_DB`
(`VIDUSHI_DATA_DIR` relocates the snapshot/import dir).

## Conventions
- One concern per call; let the script do the filtering — don't pull the whole store back.
- `acct` is `personal` or `business` (business = bought on `antojk@anthilllabs.in`, usually GST).
- Saved document copies live under `../documents/<acct>/<vendor>/`; the JSONL row's `file` points to them.
- Extend with more scripts here (e.g. an expense/tax summarizer) as needs grow — keep them JSON-out.
