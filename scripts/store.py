#!/usr/bin/env python3
"""Office-assistant JSONL data store CLI.

Token-frugal access to the office_assistant data stores. Agents call this instead of
reading whole JSONL files into context, and project only the fields they need.

Stores (../data/*.jsonl) — see ../data/schema.md:
  contacts    vendor_contacts.jsonl   verified vendor support contacts
  invoices    invoices.jsonl          purchase documents: po | invoice | receipt | creditnote
  warranties  warranties.jsonl        warranty coverage + expiry
  cases       support_cases.jsonl     support / claim / RMA cases
  products    product_catalogue.jsonl owned-product knowledge base

CRUD:
  store.py query <type> [--where f=v ...] [--contains f=sub ...] [--after f=D] [--before f=D] [--fields a,b.c] [--sort f] [--limit N] [--expand fk,fk]
  store.py get <type> <id> [--fields ...] [--expand fk,fk]
  store.py add <type> --json '{...}'          # id/updated auto-filled if absent; array = bulk
  store.py update <type> <id> --json '{...}'  # shallow-merge patch (+ --append-log "note" for cases)
  store.py rm <type> <id>
  store.py stats <type> [--by field]

Tracking-state framework (see schema.md "Tracking state framework"):
  store.py set-status <type> <STATUS> (--id ID | --where f=v ...)   # lifecycle: NEW|UNKNOWN|IN_PROGRESS|COMPLETED|EXPIRED
  store.py action-add <type> <id> <slug> [--detail T] [--owner user|agent] [--due D]   # open a domain action
  store.py action-resolve <type> <id> <slug>                       # flip an OPEN action -> RESOLVED
  store.py doc-add <type> <id> <asset-type> <path> [--number N] [--date D]   # attach a domain document asset
  store.py attention [<type>]                                      # records needing attention (OPEN actions / NEW / UNKNOWN / EXPIRED)
  store.py warranty-sweep [--dry-run]                              # recompute ACTIVE->EXPIRED from expiry; auto-open renew-or-extend

Fields support dotted paths (e.g. source.email_id). Output is compact JSON on stdout; warnings to stderr.
"""
import argparse, json, os, sys, datetime, re

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("OA_DATA_DIR") or os.path.normpath(os.path.join(HERE, "..", "data"))
STORES = {"contacts": "vendor_contacts.jsonl", "invoices": "invoices.jsonl",
          "warranties": "warranties.jsonl", "cases": "support_cases.jsonl",
          "products": "product_catalogue.jsonl",
          "subscriptions": "subscriptions.jsonl", "insurance": "insurance.jsonl"}
PREFIX = {"contacts": "ven", "invoices": "doc", "warranties": "war", "cases": "case",
          "products": "prod", "subscriptions": "sub", "insurance": "ins"}
# Foreign keys: field name -> store it references. `--expand` resolves them inline.
FK_MAP = {"contact_id": "contacts", "invoice_id": "invoices",
          "warranty_id": "warranties", "product_id": "products",
          "subscription_id": "subscriptions"}

# ── Tracking-state framework ──────────────────────────────────────────────────
# Shared lifecycle vocabulary across every domain; `warranties` adds EXPIRED
# (the ACTIVE->EXPIRED refinement). null/absent status is treated as UNKNOWN.
STATUSES = ["NEW", "UNKNOWN", "IN_PROGRESS", "COMPLETED", "EXPIRED"]
TERMINAL = {"COMPLETED"}                       # dropped from routine re-tracking (unless an action is still OPEN)
ATTENTION_STATUSES = {"NEW", "UNKNOWN", "EXPIRED"}  # surfaced by `attention` even with no OPEN action
ACTION_STATUSES = ["OPEN", "RESOLVED"]         # an action runs OPEN -> RESOLVED
# Domain-specific ACTION vocabularies (advisory: unknown slugs warn but are allowed).
ACTION_SETS = {
    "invoices":   ["payment", "shipment", "in-transit", "out-for-delivery", "delivery",
                   "customs-clearance", "duty-payment", "kyc", "return", "refund", "tax-invoice"],
    "warranties": ["register-product", "capture-serial", "confirm-term", "confirm-warranty-start",
                   "renew-or-extend", "expiry-reminder", "warranty-query"],
    "cases":      ["raise-ticket", "rma-issue", "ship-back", "repair", "replace", "resolution-confirm"],
    "products":   [],
    "contacts":   [],
    "subscriptions": ["renewal-confirm", "cancel-before-charge", "keep-tombstone-decision",
                      "de-register-mandate", "card-update", "price-change", "trial-end-cancel"],
    "insurance":  ["renew-policy", "pay-premium", "kyc", "claim", "price-compare"],
}
# Domain-specific DOCUMENT-ASSET vocabularies (advisory).
DOC_ASSETS = {
    "invoices":   ["purchase-order", "invoice", "receipt", "credit-note",
                   "customs-boe", "duty-invoice", "tax-invoice", "packing-slip"],
    "warranties": ["warranty-card", "registration", "extended-warranty", "amc"],
    "cases":      ["ticket", "rma-authorization", "service-report", "replacement-invoice"],
    "products":   ["manual", "datasheet", "spec-sheet"],
    "contacts":   [],
    "subscriptions": ["receipt", "mandate", "cancellation"],
    "insurance":  ["policy-schedule", "renewal-notice", "premium-receipt"],
}
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


# CR-OA-010 #2 — per-store minimal default field set for TOON row output. When a
# read verb runs in TOON with neither --fields nor --full, rows are projected to
# these identifying columns (id first) to keep the token cost low. `id` is always
# included. A default key missing from a given doc is simply skipped.
DEFAULT_FIELDS = {
    "products": ["id", "product", "manufacturer", "category"],
    "subscriptions": ["id", "provider", "disposition", "status", "renews"],
    "invoices": ["id", "vendor", "number", "amount", "date"],
    "warranties": ["id", "vendor", "product", "expiry", "status"],
    "contacts": ["id", "vendor", "support_email", "status"],
    "insurance": ["id", "insurer", "policy_no", "expiry", "status"],
    "cases": ["id", "vendor", "issue", "status"],
}

# CR-OA-010 #3 — TOON string values longer than this cap are truncated to
# `<first CAP chars>…(+N chars)` so a single long field can't blow up a row.
_TRUNC_CAP = 80


def _truncate(v):
    """Truncate an over-long string to the CAP with a `…(+N chars)` size hint;
    non-strings and short strings pass through untouched."""
    if isinstance(v, str) and len(v) > _TRUNC_CAP:
        return v[:_TRUNC_CAP] + f"…(+{len(v) - _TRUNC_CAP} chars)"
    return v


def _toon_shape(rec, type_):
    """TOON-only row shaping (CR-OA-010 #2 + #3): project to DEFAULT_FIELDS for
    the store type (keeping the declared order, skipping absent keys), then
    truncate each long string value. Callers gate this on
    `_FMT == "toon" and not --full and not --fields`."""
    keys = DEFAULT_FIELDS.get(type_)
    shaped = {k: rec[k] for k in keys if k in rec} if keys else dict(rec)
    return {k: _truncate(v) for k, v in shaped.items()}


def expand(rec, fields):
    import oa_mongo
    for f in fields:
        store = FK_MAP.get(f)
        ref = getp(rec, f)
        if store and ref:
            rec[f + "_obj"] = oa_mongo.coll(store).find_one({"id": ref}, {"_id": 0})
    return rec


# Resolved stdout encoding for the current invocation (set in main()); every
# verb prints through out(). "toon" is the default; "json" preserves the exact
# pre-CR compact JSON. Does NOT affect the data/*.jsonl snapshot writer, which
# stays JSON for chezmoi.
_FMT = "toon"


def out(obj):
    if _FMT == "toon":
        import oa_toon
        print(oa_toon.to_toon(obj))
    else:
        print(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))


def find(rows, rid):
    for r in rows:
        if r.get("id") == rid:
            return r
    return None


def _open_actions(r):
    return [x.get("action") for x in (r.get("actions") or []) if x.get("status") == "OPEN"]


def gen_id(t, rec, existing):
    if t == "products":
        anchor = rec.get("manufacturer")
    elif t == "subscriptions":
        anchor = rec.get("provider")
    elif t == "insurance":
        anchor = rec.get("insurer")
    else:
        anchor = rec.get("vendor")
    base = PREFIX[t] + "_" + (slug(anchor) or "x")
    if t == "invoices":
        base += "_" + (slug(rec.get("number") or rec.get("date")) or "x")
    elif t == "products":
        base += "_" + (slug(rec.get("model") or rec.get("product")) or "x")
    elif t == "insurance" and rec.get("policy_no"):
        base += "_" + slug(rec.get("policy_no"))
    cand, i = base, 2
    while cand in existing:
        cand = f"{base}-{i}"
        i += 1
    return cand


def _coerce_scalar(v):
    """Coerce a `--where` string value to its Mongo-comparable type."""
    if v == "true":
        return True
    if v == "false":
        return False
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v


def _mongo_filter(a):
    """Translate the query flags into a MongoDB filter document (all AND-ed)."""
    f = {}
    for w in (a.where or []):
        k, _, v = w.partition("=")
        if v in ("None", "null"):
            f[k] = {"$in": [None]}          # null-or-missing
        else:
            f[k] = _coerce_scalar(v)
    for c in (a.contains or []):
        k, _, sub = c.partition("=")
        f[k] = {"$regex": re.escape(sub), "$options": "i"}  # matches strings + array-of-string elements
    for w in (getattr(a, "after", None) or []):
        k, _, d = w.partition("=")
        cur = f.get(k)
        if not isinstance(cur, dict):
            cur = f[k] = {}
        cur["$gte"] = d                     # ISO date >= bound (inclusive)
    for w in (getattr(a, "before", None) or []):
        k, _, d = w.partition("=")
        cur = f.get(k)
        if not isinstance(cur, dict):
            cur = f[k] = {}
        cur["$lte"] = d                     # ISO date <= bound (inclusive)
    if getattr(a, "filter", None):
        f.update(json.loads(a.filter))      # native Mongo passthrough
    return f


def cmd_query(a):
    import oa_mongo
    docs = list(oa_mongo.coll(a.type).find(_mongo_filter(a), {"_id": 0}))
    if a.sort:
        docs.sort(key=lambda r: (getp(r, a.sort) is None, str(getp(r, a.sort))))
    if a.limit:
        docs = docs[:a.limit]
    if a.expand:
        exp = a.expand.split(",")
        docs = [expand(r, exp) for r in docs]
    fields = a.fields.split(",") if a.fields else None
    rows = [project(r, fields) for r in docs]
    if _FMT == "toon" and not getattr(a, "full", False) and not fields:
        rows = [_toon_shape(r, a.type) for r in rows]
    if _FMT == "toon":
        out({"count": len(rows), "results": rows, "next": _query_next(a.type, rows)})
    else:
        out(rows)


def _query_next(type_, rows):
    """Contextual follow-up command templates for a TOON `query` envelope
    (CR-OA-010 Cycle B #9): a concise 1-3 entry list, always referencing the
    queried store type."""
    nxt = []
    if rows and rows[0].get("id"):
        nxt.append(f"get {type_} {rows[0]['id']}")
    nxt.append(f"query {type_} --where <field>=<value>")
    return nxt[:3]


def cmd_get(a):
    import oa_mongo
    r = oa_mongo.coll(a.type).find_one({"id": a.id}, {"_id": 0})
    if r is None:
        return out(None)
    if a.expand:
        r = expand(r, a.expand.split(","))
    if a.fields:
        r = project(r, a.fields.split(","))
    elif _FMT == "toon" and not getattr(a, "full", False):
        r = _toon_shape(r, a.type)
    out(r)


def cmd_add(a):
    import oa_mongo
    from pymongo.errors import DuplicateKeyError
    payload = json.loads(a.json)
    recs = payload if isinstance(payload, list) else [payload]
    coll = oa_mongo.coll(a.type)
    existing = {d["id"] for d in coll.find({}, {"id": 1, "_id": 0})}
    added, skipped = [], []
    for rec in recs:
        rec["id"] = rec.get("id") or gen_id(a.type, rec, existing)
        if rec["id"] in existing:
            skipped.append(rec["id"]); continue
        rec.setdefault("updated", today())
        try:
            coll.insert_one(dict(rec))
        except DuplicateKeyError:
            skipped.append(rec["id"]); continue
        existing.add(rec["id"]); added.append(rec["id"])
    out({"added": added, "skipped": skipped})


def cmd_update(a):
    import oa_mongo
    coll = oa_mongo.coll(a.type)
    patch = json.loads(a.json) if a.json else {}
    upd = {"$set": {**patch, "updated": today()}}
    if a.append_log is not None:
        upd["$push"] = {"log": {"date": today(), "note": a.append_log}}
    res = coll.update_one({"id": a.id}, upd)
    if res.matched_count == 0:
        out({"error": "not found", "id": a.id}); sys.exit(1)
    out({"updated": a.id})


def cmd_rm(a):
    import oa_mongo
    coll = oa_mongo.coll(a.type)
    coll.delete_one({"id": a.id})
    out({"removed": a.id, "remaining": coll.count_documents({})})


def cmd_stats(a):
    import oa_mongo
    coll = oa_mongo.coll(a.type)
    total = coll.count_documents({})
    if a.by:
        counts = {}
        for doc in coll.aggregate([{"$group": {"_id": f"${a.by}", "n": {"$sum": 1}}}]):
            counts[str(doc["_id"])] = doc["n"]
        out({"type": a.type, "total": total, "by": a.by, "counts": counts})
    else:
        out({"type": a.type, "total": total})


# ── Tracking-state verbs ──────────────────────────────────────────────────────
def cmd_set_status(a):
    import oa_mongo
    status = a.status.upper()
    if status not in STATUSES:
        out({"error": "invalid status", "given": a.status, "allowed": STATUSES}); sys.exit(1)
    coll = oa_mongo.coll(a.type)
    if a.id:
        f = {"id": a.id}
    elif a.where or a.contains:
        f = _mongo_filter(a)
    else:
        out({"error": "give <id> or --where/--contains"}); sys.exit(1)
    ids = [d["id"] for d in coll.find(f, {"id": 1, "_id": 0})]
    if not ids:
        out({"error": "no targets matched"}); sys.exit(1)
    coll.update_many(f, {"$set": {"status": status, "updated": today()}})
    out({"status": status, "count": len(ids), "ids": ids})


def cmd_action_add(a):
    import oa_mongo
    coll = oa_mongo.coll(a.type)
    known = ACTION_SETS.get(a.type, [])
    if known and a.action not in known:
        sys.stderr.write(f"warn: '{a.action}' not in {a.type} action set {known}\n")
    act = {"action": a.action, "status": "OPEN", "opened": today()}
    if a.detail: act["detail"] = a.detail
    if a.owner:  act["owner"] = a.owner
    if a.due:    act["due"] = a.due
    res = coll.update_one({"id": a.id}, {"$push": {"actions": act}, "$set": {"updated": today()}})
    if res.matched_count == 0:
        out({"error": "not found", "id": a.id}); sys.exit(1)
    out({"id": a.id, "action": a.action, "status": "OPEN"})


def cmd_action_resolve(a):
    import oa_mongo
    coll = oa_mongo.coll(a.type)
    res = coll.update_one(
        {"id": a.id, "actions": {"$elemMatch": {"action": a.action, "status": "OPEN"}}},
        {"$set": {"actions.$.status": "RESOLVED", "actions.$.resolved": today(), "updated": today()}},
    )
    if res.matched_count == 0:
        out({"error": "no OPEN action", "id": a.id, "action": a.action}); sys.exit(1)
    out({"id": a.id, "action": a.action, "status": "RESOLVED"})


def cmd_doc_add(a):
    import oa_mongo
    coll = oa_mongo.coll(a.type)
    known = DOC_ASSETS.get(a.type, [])
    if known and a.asset_type not in known:
        sys.stderr.write(f"warn: '{a.asset_type}' not in {a.type} document-asset set {known}\n")
    doc = {"type": a.asset_type, "path": a.path}
    if a.number: doc["number"] = a.number
    if a.date:   doc["date"] = a.date
    res = coll.update_one({"id": a.id}, {"$push": {"documents": doc}, "$set": {"updated": today()}})
    if res.matched_count == 0:
        out({"error": "not found", "id": a.id}); sys.exit(1)
    out({"id": a.id, "document": doc})


def cmd_attention(a):
    import oa_mongo
    types = [a.type] if a.type else list(STORES.keys())
    res = []
    query = {"$or": [{"actions.status": "OPEN"}, {"status": {"$in": list(ATTENTION_STATUSES)}}]}
    for t in types:
        for d in oa_mongo.coll(t).find(query, {"_id": 0}):
            opens = _open_actions(d)
            res.append({"type": t, "id": d.get("id"),
                        "name": d.get("vendor") or d.get("product") or d.get("provider"),
                        "status": d.get("status") or "UNKNOWN", "open_actions": opens})
    out(res)


def cmd_warranty_sweep(a):
    """Recompute past-due warranties to EXPIRED via the transition engine on Mongo;
    each `expire` transition opens a renew-or-extend action. The `status != EXPIRED`
    filter makes a repeat sweep idempotent (already-expired warranties are skipped)."""
    import oa_mongo, transitions
    coll = oa_mongo.coll("warranties")
    now = today(); changed = []
    for doc in coll.find({"expiry": {"$lt": now}, "status": {"$ne": "EXPIRED"}}, {"_id": 0}):
        tr = transitions.find_transition("warranties", doc.get("status"), "expire")
        if tr is None:
            continue
        if not a.dry_run:
            _apply_transition(coll, doc, tr)
        changed.append(doc["id"])
    out({"expired": changed, "count": len(changed), "dry_run": bool(a.dry_run)})


def cmd_due_sweep(a):
    """Mark recurring-store docs (subscriptions, insurance, ...) DUE when their
    renewal trigger — EITHER `renews` OR `expiry` — falls within the 30-day
    lookahead, via the transition engine on Mongo; each `renewal-window`
    transition opens the domain action (e.g. cancel-before-charge for
    subscriptions, renew-policy for insurance, which carries `expiry` not
    `renews`). Recurring stores are discovered dynamically as those that declare a
    `renewal-window` transition. The `status != DUE` filter makes a repeat sweep
    idempotent (already-due docs are skipped)."""
    import oa_mongo, transitions
    cutoff = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()
    recurring = [t for t in STORES if transitions.find_transition(t, "IN_PROGRESS", "renewal-window")]
    due = {}
    count = 0
    for t in recurring:
        coll = oa_mongo.coll(t)
        ids = []
        for doc in coll.find({"$or": [{"renews": {"$lte": cutoff}}, {"expiry": {"$lte": cutoff}}], "status": {"$ne": "DUE"}}, {"_id": 0}):
            tr = transitions.find_transition(t, doc.get("status"), "renewal-window")
            if tr is None:
                continue
            if not a.dry_run:
                _apply_transition(coll, doc, tr)
            ids.append(doc["id"])
        due[t] = ids
        count += len(ids)
    out({"due": due, "count": count, "dry_run": bool(a.dry_run)})


def _apply_transition(coll, doc, tr):
    """Apply one declarative transition to a Mongo doc: set status->`tr["to"]`
    (+ updated), fire the transition's effects (open-action / require-doc pushes,
    resolve-action flips OPEN->RESOLVED). Shared by `event` and `warranty-sweep`."""
    now = today()
    set_fields = {"status": tr["to"], "updated": now}
    pushes = []
    resolves = []
    for effect in tr.get("effects", []):
        op = effect.get("op")
        if op == "open-action":
            act = {"action": effect.get("action"), "status": "OPEN", "opened": now}
            if effect.get("owner"):  act["owner"] = effect["owner"]
            if effect.get("detail"): act["detail"] = effect["detail"]
            pushes.append(act)
        elif op == "require-doc":
            pushes.append({"action": "archive-doc", "status": "OPEN", "opened": now,
                           "detail": f"archive {effect.get('type')} document"})
        elif op == "resolve-action":
            resolves.append(effect.get("action"))
    update = {"$set": set_fields}
    if pushes:
        update["$push"] = {"actions": {"$each": pushes}}
    coll.update_one({"id": doc["id"]}, update)
    for slug_name in resolves:
        coll.update_one(
            {"id": doc["id"], "actions": {"$elemMatch": {"action": slug_name, "status": "OPEN"}}},
            {"$set": {"actions.$.status": "RESOLVED", "actions.$.resolved": now}})


def cmd_event(a):
    """Drive a doc through the declarative transition table: look up (status, event),
    apply the matching transition (set status + fire effects), reject an unmatched
    (from, event) pair leaving the Mongo doc untouched."""
    import oa_mongo, transitions
    coll = oa_mongo.coll(a.type)
    doc = coll.find_one({"id": a.id}, {"_id": 0})
    if doc is None:
        out({"error": "not found", "id": a.id}); sys.exit(1)
    tr = transitions.find_transition(a.type, doc.get("status"), a.event)
    if tr is None:
        out({"error": "illegal transition", "id": a.id,
             "from": doc.get("status"), "event": a.event}); sys.exit(1)
    _apply_transition(coll, doc, tr)
    out({"id": a.id, "event": a.event, "from": tr["from"], "to": tr["to"]})


SCHEMA_DIR = os.path.normpath(os.path.join(HERE, "..", "data", "schema"))


def _load_schema(t):
    """Load the JSON Schema for store type `t` from data/schema/<t>.schema.json."""
    with open(os.path.join(SCHEMA_DIR, f"{t}.schema.json"), encoding="utf-8") as f:
        return json.load(f)


def _apply_validators():
    """Attach each store's `$jsonSchema` validator to its collection (idempotent)."""
    import oa_mongo
    db = oa_mongo.db()
    existing = set(db.list_collection_names())
    for t in STORES:
        if t not in existing:
            db.create_collection(t)
            existing.add(t)
        db.command("collMod", t, validator={"$jsonSchema": _load_schema(t)},
                   validationLevel="moderate", validationAction="error")
    return list(STORES)


def cmd_apply_validators(a):
    """Attach each store's `$jsonSchema` validator to its MongoDB collection (idempotent)."""
    import oa_mongo
    done = _apply_validators()
    out({"validated": done, "db": oa_mongo.db().name})


def _nonconforming_ids(t):
    """Ids of documents in collection `t` that do NOT match the store's $jsonSchema."""
    import oa_mongo
    return [d["id"] for d in oa_mongo.coll(t).find(
        {"$nor": [{"$jsonSchema": _load_schema(t)}]}, {"id": 1, "_id": 0})]


def cmd_validate(a):
    """List ids of non-conforming documents. With a <type>, print a bare id array for
    that collection; with none, print a {type: [ids], ...} object across all STORES."""
    if a.type:
        out(_nonconforming_ids(a.type))
    else:
        out({t: _nonconforming_ids(t) for t in STORES})


def cmd_import(a):
    """Read each store's JSONL from DATA (honouring OA_DATA_DIR) and upsert every
    record into Mongo by `id` (idempotent — re-running creates no duplicates)."""
    import oa_mongo
    types = [a.type] if a.type else list(STORES)
    imported = {}
    for t in types:
        coll = oa_mongo.coll(t)
        n = 0
        for rec in load(t):
            coll.replace_one({"id": rec["id"]}, rec, upsert=True)
            n += 1
        imported[t] = n
    out({"imported": imported})


def cmd_init(a):
    """Create each store's MongoDB collection + a unique index on `id`, then attach
    the `$jsonSchema` validators (idempotent)."""
    import oa_mongo
    done = []
    for t in STORES:
        oa_mongo.coll(t).create_index("id", unique=True)
        done.append(t)
    _apply_validators()
    out({"initialized": done, "db": oa_mongo.db().name})


def cmd_snapshot(a):
    """Export each store's Mongo collection back to its JSONL file under DATA
    (honouring OA_DATA_DIR). One JSON object per line, `_id` stripped, keys ordered
    `id` first then the rest sorted -> byte-identical output across repeated runs.
    Writes atomically (tmp file + os.replace)."""
    import oa_mongo
    types = [a.type] if a.type else list(STORES)
    counts = {}
    for t in types:
        docs = list(oa_mongo.coll(t).find({}, {"_id": 0}))
        target = path(t)
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for d in docs:
                ordered = {"id": d.get("id"), **{k: d[k] for k in sorted(d) if k != "id"}}
                f.write(json.dumps(ordered, ensure_ascii=False) + "\n")
        os.replace(tmp, target)
        counts[t] = len(docs)
    out({"snapshot": counts})


def main():
    p = argparse.ArgumentParser(description="Office-assistant JSONL store")
    sub = p.add_subparsers(dest="cmd", required=True)
    # Global --format toon|json (default toon), available AFTER every verb via a
    # shared parent parser. Read verbs additionally accept a bare --json shortcut
    # (write verbs already own --json for their input payload).
    fmt = argparse.ArgumentParser(add_help=False)
    fmt.add_argument("--format", choices=["toon", "json"], default=None, dest="format")

    def add_parser(name, **kw):
        return sub.add_parser(name, parents=[fmt], **kw)

    def with_type(sp):
        sp.add_argument("type", choices=STORES.keys())

    def read_json(sp):
        sp.add_argument("--json", action="store_true", dest="json_out",
                        help="emit strict JSON instead of the default TOON")

    def read_full(sp):
        sp.add_argument("--full", action="store_true", dest="full",
                        help="TOON: show every field, untruncated (disable the default minimal projection)")

    q = add_parser("query"); with_type(q); read_json(q); read_full(q)
    q.add_argument("--where", action="append"); q.add_argument("--contains", action="append")
    q.add_argument("--after", action="append",
                   help="FIELD=YYYY-MM-DD: keep rows where ISO date FIELD >= value (inclusive); repeatable, dotted paths ok")
    q.add_argument("--before", action="append",
                   help="FIELD=YYYY-MM-DD: keep rows where ISO date FIELD <= value (inclusive); repeatable")
    q.add_argument("--fields"); q.add_argument("--sort"); q.add_argument("--limit", type=int)
    q.add_argument("--filter", help="native MongoDB filter as a JSON object, AND-merged with the other flags")
    q.add_argument("--expand", help="comma list of FK fields to resolve inline (e.g. contact_id,invoice_id)")
    q.set_defaults(func=cmd_query)
    g = add_parser("get"); with_type(g); g.add_argument("id"); read_json(g); read_full(g)
    g.add_argument("--expand"); g.add_argument("--fields"); g.set_defaults(func=cmd_get)
    ad = add_parser("add"); with_type(ad); ad.add_argument("--json", required=True); ad.set_defaults(func=cmd_add)
    up = add_parser("update"); with_type(up); up.add_argument("id")
    up.add_argument("--json"); up.add_argument("--append-log", dest="append_log"); up.set_defaults(func=cmd_update)
    rm = add_parser("rm"); with_type(rm); rm.add_argument("id"); rm.set_defaults(func=cmd_rm)
    st = add_parser("stats"); with_type(st); read_json(st); read_full(st); st.add_argument("--by"); st.set_defaults(func=cmd_stats)

    # tracking-state framework
    ss = add_parser("set-status"); with_type(ss)
    ss.add_argument("status", help="one of: " + " ".join(STATUSES))
    ss.add_argument("--id"); ss.add_argument("--where", action="append"); ss.add_argument("--contains", action="append")
    ss.set_defaults(func=cmd_set_status)
    aa = add_parser("action-add"); with_type(aa); aa.add_argument("id"); aa.add_argument("action")
    aa.add_argument("--detail"); aa.add_argument("--owner", choices=["user", "agent"]); aa.add_argument("--due")
    aa.set_defaults(func=cmd_action_add)
    arv = add_parser("action-resolve"); with_type(arv); arv.add_argument("id"); arv.add_argument("action")
    arv.set_defaults(func=cmd_action_resolve)
    da = add_parser("doc-add"); with_type(da); da.add_argument("id"); da.add_argument("asset_type"); da.add_argument("path")
    da.add_argument("--number"); da.add_argument("--date"); da.set_defaults(func=cmd_doc_add)
    at = add_parser("attention"); at.add_argument("type", nargs="?", choices=STORES.keys()); read_json(at); read_full(at)
    at.set_defaults(func=cmd_attention)
    ws = add_parser("warranty-sweep"); ws.add_argument("--dry-run", action="store_true", dest="dry_run")
    ws.set_defaults(func=cmd_warranty_sweep)
    ds = add_parser("due-sweep"); ds.add_argument("--dry-run", action="store_true", dest="dry_run")
    ds.set_defaults(func=cmd_due_sweep)
    ev = add_parser("event"); with_type(ev); ev.add_argument("id"); ev.add_argument("event")
    ev.set_defaults(func=cmd_event)
    va = add_parser("validate"); va.add_argument("type", nargs="?", choices=STORES.keys()); read_json(va); read_full(va)
    va.set_defaults(func=cmd_validate)
    im = add_parser("import"); im.add_argument("type", nargs="?", choices=STORES.keys())
    im.set_defaults(func=cmd_import)
    sn = add_parser("snapshot"); sn.add_argument("type", nargs="?", choices=STORES.keys())
    sn.set_defaults(func=cmd_snapshot)
    it = add_parser("init"); it.set_defaults(func=cmd_init)
    av = add_parser("apply-validators"); av.set_defaults(func=cmd_apply_validators)

    # Content-first no-arg path (S6/AXI #8): a truly-empty argv (just the
    # program name) prints live data — a one-line description + the executable
    # path token, then the `attention` worklist — instead of letting argparse
    # error with `usage:`. `-h`/`--help` still reaches argparse (handled there).
    global _FMT
    if len(sys.argv) == 1:
        env = os.environ.get("OA_FORMAT")
        _FMT = env if env in ("toon", "json") else "toon"
        print("office_assistant store (scripts/store.py) — MongoDB-backed "
              "personal-admin data CLI. Rows needing attention:")
        cmd_attention(argparse.Namespace(type=None))
        return

    a = p.parse_args()
    fmt_choice = getattr(a, "format", None)          # explicit flag wins
    if fmt_choice is None:
        if getattr(a, "json_out", False):            # --json read shortcut
            fmt_choice = "json"
        else:
            env = os.environ.get("OA_FORMAT")
            fmt_choice = env if env in ("toon", "json") else "toon"   # env, else default; garbage env -> toon
    _FMT = fmt_choice
    a.func(a)


if __name__ == "__main__":
    main()
