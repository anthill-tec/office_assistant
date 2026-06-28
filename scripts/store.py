#!/usr/bin/env python3
"""Office-assistant JSONL data store CLI.

Token-frugal access to the office_assistant data stores. Agents call this instead of
reading whole JSONL files into context, and project only the fields they need.

Stores (../data/*.jsonl) — see ../data/schema.md:
  contacts    vendor_contacts.jsonl   verified vendor support contacts
  invoices    invoices.jsonl          purchase documents: po | invoice | receipt | creditnote
  warranties  warranties.jsonl        warranty coverage + expiry
  cases       support_cases.jsonl     support / claim / RMA cases

Usage:
  store.py query <type> [--where f=v ...] [--contains f=sub ...] [--fields a,b.c] [--sort f] [--limit N]
  store.py get <type> <id>
  store.py add <type> --json '{...}'         # id/updated auto-filled if absent
  store.py update <type> <id> --json '{...}'  # shallow-merge patch (+ --append-log "note" for cases)
  store.py rm <type> <id>
  store.py stats <type> [--by field]
Fields support dotted paths (e.g. source.email_id). Output is compact JSON.
"""
import argparse, json, os, sys, datetime, re

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.normpath(os.path.join(HERE, "..", "data"))
STORES = {"contacts": "vendor_contacts.jsonl", "invoices": "invoices.jsonl",
          "warranties": "warranties.jsonl", "cases": "support_cases.jsonl",
          "products": "product_catalogue.jsonl"}
PREFIX = {"contacts": "ven", "invoices": "doc", "warranties": "war", "cases": "case",
          "products": "prod"}
# Foreign keys: field name -> store it references. `--expand` resolves them inline.
FK_MAP = {"contact_id": "contacts", "invoice_id": "invoices",
          "warranty_id": "warranties", "product_id": "products"}
_CACHE = {}


def path(t):
    return os.path.join(DATA, STORES[t])


def load(t):
    p = path(t)
    if not os.path.exists(p):
        return []
    rows = []
    with open(p, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as e:
                sys.stderr.write(f"warn: {STORES[t]}:{n} bad JSON ({e}); skipped\n")
    return rows


def save(t, rows):
    tmp = path(t) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path(t))


def today():
    return datetime.date.today().isoformat()


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", str(s or "").lower()).strip("-")


def getp(rec, dotted):
    cur = rec
    for k in dotted.split("."):
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            return None
    return cur


def project(rec, fields):
    return rec if not fields else {f: getp(rec, f) for f in fields}


def _index(t):
    if t not in _CACHE:
        _CACHE[t] = {r.get("id"): r for r in load(t)}
    return _CACHE[t]


def expand(rec, fields):
    for f in fields:
        store = FK_MAP.get(f)
        ref = getp(rec, f)
        if store and ref:
            rec[f + "_obj"] = _index(store).get(ref)
    return rec


def out(obj):
    print(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))


def gen_id(t, rec, existing):
    anchor = rec.get("manufacturer") if t == "products" else rec.get("vendor")
    base = PREFIX[t] + "_" + (slug(anchor) or "x")
    if t == "invoices":
        base += "_" + (slug(rec.get("number") or rec.get("date")) or "x")
    elif t == "products":
        base += "_" + (slug(rec.get("model") or rec.get("product")) or "x")
    cand, i = base, 2
    while cand in existing:
        cand = f"{base}-{i}"
        i += 1
    return cand


def cmd_query(a):
    rows = load(a.type)
    def ok(r):
        for w in (a.where or []):
            k, _, v = w.partition("=")
            if str(getp(r, k)) != v:
                return False
        for c in (a.contains or []):
            k, _, sub = c.partition("=")
            val = getp(r, k)
            if val is None or sub.lower() not in str(val).lower():
                return False
        return True
    res = [r for r in rows if ok(r)]
    if a.sort:
        res.sort(key=lambda r: (getp(r, a.sort) is None, str(getp(r, a.sort))))
    if a.limit:
        res = res[:a.limit]
    if a.expand:
        exp = a.expand.split(",")
        res = [expand(r, exp) for r in res]
    fields = a.fields.split(",") if a.fields else None
    out([project(r, fields) for r in res])


def cmd_get(a):
    for r in load(a.type):
        if r.get("id") == a.id:
            if a.expand:
                r = expand(r, a.expand.split(","))
            if a.fields:
                r = project(r, a.fields.split(","))
            return out(r)
    out(None)


def cmd_add(a):
    payload = json.loads(a.json)
    recs = payload if isinstance(payload, list) else [payload]
    rows = load(a.type)
    existing = {r.get("id") for r in rows}
    added, skipped = [], []
    for rec in recs:
        rec["id"] = rec.get("id") or gen_id(a.type, rec, existing)
        if rec["id"] in existing:
            skipped.append(rec["id"]); continue
        rec.setdefault("updated", today())
        rows.append(rec); existing.add(rec["id"]); added.append(rec["id"])
    save(a.type, rows)
    out({"added": added, "skipped": skipped})


def cmd_update(a):
    rows = load(a.type); patch = json.loads(a.json) if a.json else {}
    hit = None
    for r in rows:
        if r.get("id") == a.id:
            r.update(patch)
            if a.append_log is not None:
                r.setdefault("log", []).append({"date": today(), "note": a.append_log})
            r["updated"] = today(); hit = r; break
    if not hit:
        out({"error": "not found", "id": a.id}); sys.exit(1)
    save(a.type, rows); out({"updated": a.id})


def cmd_rm(a):
    rows = load(a.type); new = [r for r in rows if r.get("id") != a.id]
    save(a.type, new); out({"removed": a.id, "remaining": len(new)})


def cmd_stats(a):
    rows = load(a.type)
    if a.by:
        counts = {}
        for r in rows:
            key = str(getp(r, a.by))
            counts[key] = counts.get(key, 0) + 1
        out({"type": a.type, "total": len(rows), "by": a.by, "counts": counts})
    else:
        out({"type": a.type, "total": len(rows)})


def main():
    p = argparse.ArgumentParser(description="Office-assistant JSONL store")
    sub = p.add_subparsers(dest="cmd", required=True)
    def with_type(sp):
        sp.add_argument("type", choices=STORES.keys())
    q = sub.add_parser("query"); with_type(q)
    q.add_argument("--where", action="append"); q.add_argument("--contains", action="append")
    q.add_argument("--fields"); q.add_argument("--sort"); q.add_argument("--limit", type=int)
    q.add_argument("--expand", help="comma list of FK fields to resolve inline (e.g. contact_id,invoice_id)")
    q.set_defaults(func=cmd_query)
    g = sub.add_parser("get"); with_type(g); g.add_argument("id")
    g.add_argument("--expand"); g.add_argument("--fields"); g.set_defaults(func=cmd_get)
    ad = sub.add_parser("add"); with_type(ad); ad.add_argument("--json", required=True); ad.set_defaults(func=cmd_add)
    up = sub.add_parser("update"); with_type(up); up.add_argument("id")
    up.add_argument("--json"); up.add_argument("--append-log", dest="append_log"); up.set_defaults(func=cmd_update)
    rm = sub.add_parser("rm"); with_type(rm); rm.add_argument("id"); rm.set_defaults(func=cmd_rm)
    st = sub.add_parser("stats"); with_type(st); st.add_argument("--by"); st.set_defaults(func=cmd_stats)
    a = p.parse_args(); a.func(a)


if __name__ == "__main__":
    main()
