# Office Assistant — Scripts

Token-frugal helpers over the `data/*.jsonl` stores. **Skills and agents MUST go through these
instead of reading whole JSONL files into context** — query for exactly the rows/fields needed.

## `store.py` — JSONL data store CLI (stdlib only, `python3`)

Types: `contacts` · `invoices` · `warranties` · `cases` (schema in `../data/schema.md`).

```bash
# look up a vendor's verified support contact (only the fields you need)
python3 scripts/store.py query contacts --where vendor=LionCircuits --fields support_email,portal,rma_process

# open purchase documents missing a saved file copy
python3 scripts/store.py query invoices --where file=None --fields id,vendor,number,date,source.email_id

# add a record (id + updated auto-filled); --json is a JSON object matching the schema
python3 scripts/store.py add invoices --json '{"doc_type":"invoice","vendor":"...","date":"2026-..","amount":0,"acct":"personal","source":{"mailbox":"FM","email_id":"..."}}'

# fetch / patch / log / remove / count
python3 scripts/store.py get cases case_acme_1
python3 scripts/store.py update cases case_acme_1 --json '{"status":"awaiting_support"}' --append-log "Sent RMA request"
python3 scripts/store.py rm invoices doc_x
python3 scripts/store.py stats invoices --by acct
```

Filters: `--where field=value` (exact), `--contains field=substr` (case-insensitive); both accept
**dotted paths** (`source.email_id`, `registration.done`). `--fields a,b,c` projects; `--sort`, `--limit`.
Output is compact JSON on stdout; warnings go to stderr; writes are atomic (temp + replace).

## Conventions
- One concern per call; let the script do the filtering — don't pull the whole store back.
- `acct` is `personal` or `business` (business = bought on `antojk@anthilllabs.in`, usually GST).
- Saved document copies live under `../documents/<acct>/<vendor>/`; the JSONL row's `file` points to them.
- Extend with more scripts here (e.g. an expense/tax summarizer) as needs grow — keep them stdlib + JSON-out.
