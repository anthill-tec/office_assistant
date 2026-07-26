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
  store.py delivery-sweep [--dry-run]                              # open stuck-chase on stalled orders (last_event >7d ago or eta past)

Fields support dotted paths (e.g. source.email_id). Output is compact JSON on stdout; warnings to stderr.
"""
import argparse, json, os, sys, datetime, re, getpass

# Module-level seam: tests monkeypatch `vidushi_oa._cli.build_client`; the
# `cmd_mail_*` handlers call it (no args) to obtain a wired `MailClient`.
from vidushi_oa.mail.base import SOURCE_TAGS
from vidushi_oa.mail.factory import build_client

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("VIDUSHI_DATA_DIR") or os.path.normpath(os.path.join(HERE, "..", "data"))
STORES = {"contacts": "vendor_contacts.jsonl", "invoices": "invoices.jsonl",
          "warranties": "warranties.jsonl", "cases": "support_cases.jsonl",
          "products": "product_catalogue.jsonl",
          "subscriptions": "subscriptions.jsonl", "insurance": "insurance.jsonl",
          "orders": "orders.jsonl"}
PREFIX = {"contacts": "ven", "invoices": "doc", "warranties": "war", "cases": "case",
          "products": "prod", "subscriptions": "sub", "insurance": "ins",
          "orders": "ord"}
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
    "insurance":  ["renew-policy", "pay-premium", "kyc", "claim", "price-compare",
                   "renew-registration", "fitness-test"],
    "orders":     ["payment", "shipment", "in-transit", "out-for-delivery", "delivery",
                   "customs-clearance", "duty-payment", "kyc", "clarification",
                   "redelivery", "return", "refund", "stuck-chase"],
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
    "contacts": ["id", "vendor", "kind", "category"],
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
    from vidushi_oa.backends import get_backend, query as Q
    for f in fields:
        store = FK_MAP.get(f)
        ref = getp(rec, f)
        if store and ref:
            rec[f + "_obj"] = get_backend().store(store).find_one(Q.cond("id", "eq", ref))
    return rec


# Resolved stdout encoding for the current invocation (set in main()); every
# verb prints through out(). "toon" is the default; "json" preserves the exact
# pre-CR compact JSON. Does NOT affect the data/*.jsonl snapshot writer, which
# stays JSON for chezmoi.
_FMT = "toon"


def out(obj):
    if _FMT == "toon":
        from vidushi_oa import toon as oa_toon
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
    elif t == "orders":
        anchor = rec.get("merchant")
    else:
        anchor = rec.get("vendor")
    base = PREFIX[t] + "_" + (slug(anchor) or "x")
    if t == "invoices":
        base += "_" + (slug(rec.get("number") or rec.get("date")) or "x")
    elif t == "orders":
        base += "_" + (slug(rec.get("number") or rec.get("order_date") or rec.get("date")) or "x")
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
    """Translate the query flags into a neutral filter node plus an optional native `extra`.

    Returns `(Q.all_(*conds), native_extra)`: `--where`/`--contains`/`--after`/`--before`
    become neutral `Cond`s AND-ed together; `--filter` (native Mongo passthrough) is returned
    verbatim as `native_extra` for the backend to AND in, else None."""
    from vidushi_oa.backends import query as Q
    conds = []
    for w in (a.where or []):
        k, _, v = w.partition("=")
        if v in ("None", "null"):
            conds.append(Q.cond(k, "eq", None))          # null-or-missing
        else:
            conds.append(Q.cond(k, "eq", _coerce_scalar(v)))
    for c in (a.contains or []):
        k, _, sub = c.partition("=")
        conds.append(Q.cond(k, "contains", sub))         # matches strings + array-of-string elements
    for w in (getattr(a, "after", None) or []):
        k, _, d = w.partition("=")
        conds.append(Q.cond(k, "gte", d))                # ISO date >= bound (inclusive)
    for w in (getattr(a, "before", None) or []):
        k, _, d = w.partition("=")
        conds.append(Q.cond(k, "lte", d))                # ISO date <= bound (inclusive)
    extra = json.loads(a.filter) if getattr(a, "filter", None) else None  # native Mongo passthrough
    return Q.all_(*conds), extra


def cmd_query(a):
    from vidushi_oa.backends import get_backend
    query, extra = _mongo_filter(a)
    docs = get_backend().store(a.type).find(query, extra=extra)
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
        out({"count": len(rows), "tally": _query_tally(docs), "results": rows, "next": _query_next(a.type, rows)})
    else:
        out(rows)


def _query_tally(docs):
    """By-status tally (+ acct/disposition when present) for the TOON query
    envelope (AXI #4), derived from the already-fetched docs — no extra query.
    status always present (missing -> UNKNOWN, so it sums to count); acct /
    disposition included only when at least one doc carries them."""
    status = {}
    for d in docs:
        st = d.get("status") or "UNKNOWN"
        status[st] = status.get(st, 0) + 1
    tally = {"status": status}
    for axis in ("acct", "disposition"):
        m = {}
        for d in docs:
            v = d.get(axis)
            if v is not None:
                m[v] = m.get(v, 0) + 1
        if m:
            tally[axis] = m
    return tally


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
    from vidushi_oa.backends import get_backend, query as Q
    r = get_backend().store(a.type).find_one(Q.cond("id", "eq", a.id))
    if r is None:
        out({"error": "not found", "type": a.type, "id": a.id}); sys.exit(1)
    if a.expand:
        r = expand(r, a.expand.split(","))
    if a.fields:
        r = project(r, a.fields.split(","))
    elif _FMT == "toon" and not getattr(a, "full", False):
        r = _toon_shape(r, a.type)
    if _FMT == "toon":
        out({"result": r, "next": _get_next(a.type, r)})
    else:
        out(r)


def _get_next(type_, rec):
    """Next-step templates for a TOON `get` (AXI #9)."""
    nxt = []
    if rec.get("id"):
        nxt.append(f"update {type_} {rec['id']} --json '{{...}}'")
    nxt.append(f"query {type_} --where <field>=<value>")
    return nxt[:3]


def cmd_add(a):
    from vidushi_oa.backends import get_backend, query as Q
    backend = get_backend()
    store = backend.store(a.type)
    dup_error = backend.dup_error
    payload = json.loads(a.json)
    recs = payload if isinstance(payload, list) else [payload]
    existing = {d["id"] for d in store.find(Q.ALL, fields=["id"])}
    added, skipped = [], []
    for rec in recs:
        rec["id"] = rec.get("id") or gen_id(a.type, rec, existing)
        if rec["id"] in existing:
            skipped.append(rec["id"]); continue
        rec.setdefault("updated", today())
        try:
            store.insert(rec)
        except dup_error:
            skipped.append(rec["id"]); continue
        existing.add(rec["id"]); added.append(rec["id"])
    out({"added": added, "skipped": skipped})


def cmd_update(a):
    from vidushi_oa.backends import get_backend, query as Q
    store = get_backend().store(a.type)
    patch = json.loads(a.json) if a.json else {}
    push = {"log": [{"date": today(), "note": a.append_log}]} if a.append_log is not None else {}
    matched = store.update(Q.cond("id", "eq", a.id),
                           Q.Update(set={**patch, "updated": today()}, push=push))
    if matched == 0:
        out({"error": "not found", "id": a.id}); sys.exit(1)
    out({"updated": a.id})


def cmd_rm(a):
    from vidushi_oa.backends import get_backend, query as Q
    store = get_backend().store(a.type)
    store.delete(Q.cond("id", "eq", a.id))
    out({"removed": a.id, "remaining": store.count(Q.ALL)})


def cmd_stats(a):
    from vidushi_oa.backends import get_backend, query as Q
    store = get_backend().store(a.type)
    total = store.count(Q.ALL)
    if a.by:
        counts = store.count_by(a.by)
        env = {"type": a.type, "total": total, "by": a.by, "counts": counts}
    else:
        env = {"type": a.type, "total": total}
    if _FMT == "toon":
        env["next"] = [f"query {a.type} --where <field>=<value>", f"stats {a.type} --by <field>"]
    out(env)


# ── Tracking-state verbs ──────────────────────────────────────────────────────
def cmd_set_status(a):
    from vidushi_oa.backends import get_backend, query as Q
    status = a.status.upper()
    if status not in STATUSES:
        out({"error": "invalid status", "given": a.status, "allowed": STATUSES}); sys.exit(1)
    store = get_backend().store(a.type)
    if a.id:
        query = Q.cond("id", "eq", a.id)
        many = False
    elif a.where or a.contains:
        query, _ = _mongo_filter(a)
        many = True
    else:
        out({"error": "give <id> or --where/--contains"}); sys.exit(1)
    ids = [d["id"] for d in store.find(query, fields=["id"])]
    if not ids:
        out({"error": "no targets matched"}); sys.exit(1)
    store.update(query, Q.Update(set={"status": status, "updated": today()}), many=many)
    out({"status": status, "count": len(ids), "ids": ids})


def cmd_action_add(a):
    from vidushi_oa.backends import get_backend, query as Q
    store = get_backend().store(a.type)
    known = ACTION_SETS.get(a.type, [])
    if known and a.action not in known:
        sys.stderr.write(f"warn: '{a.action}' not in {a.type} action set {known}\n")
    act = {"action": a.action, "status": "OPEN", "opened": today()}
    if a.detail: act["detail"] = a.detail
    if a.owner:  act["owner"] = a.owner
    if a.due:    act["due"] = a.due
    matched = store.update(Q.cond("id", "eq", a.id),
                           Q.Update(set={"updated": today()}, push={"actions": [act]}))
    if matched == 0:
        out({"error": "not found", "id": a.id}); sys.exit(1)
    out({"id": a.id, "action": a.action, "status": "OPEN"})


def cmd_action_resolve(a):
    from vidushi_oa.backends import get_backend, query as Q
    store = get_backend().store(a.type)
    matched = store.update(
        Q.cond("id", "eq", a.id),
        Q.Update(set={"updated": today()},
                 resolve=("actions",
                          (Q.cond("action", "eq", a.action), Q.cond("status", "eq", "OPEN")),
                          {"status": "RESOLVED", "resolved": today()})))
    if matched == 0:
        out({"error": "no OPEN action", "id": a.id, "action": a.action}); sys.exit(1)
    out({"id": a.id, "action": a.action, "status": "RESOLVED"})


def cmd_doc_add(a):
    from vidushi_oa.backends import get_backend, query as Q
    store = get_backend().store(a.type)
    known = DOC_ASSETS.get(a.type, [])
    if known and a.asset_type not in known:
        sys.stderr.write(f"warn: '{a.asset_type}' not in {a.type} document-asset set {known}\n")
    doc = {"type": a.asset_type, "path": a.path}
    if a.number: doc["number"] = a.number
    if a.date:   doc["date"] = a.date
    matched = store.update(Q.cond("id", "eq", a.id),
                           Q.Update(set={"updated": today()}, push={"documents": [doc]}))
    if matched == 0:
        out({"error": "not found", "id": a.id}); sys.exit(1)
    out({"id": a.id, "document": doc})


def cmd_attention(a):
    from vidushi_oa.backends import get_backend, query as Q
    types = [a.type] if a.type else list(STORES.keys())
    res = []
    query = Q.any_(Q.elem("actions", Q.cond("status", "eq", "OPEN")),
                   Q.cond("status", "in", list(ATTENTION_STATUSES)))
    for t in types:
        for d in get_backend().store(t).find(query):
            opens = _open_actions(d)
            res.append({"type": t, "id": d.get("id"),
                        "name": d.get("vendor") or d.get("product") or d.get("provider") or d.get("merchant"),
                        "status": d.get("status") or "UNKNOWN", "open_actions": opens})
    if _FMT == "toon":
        t = a.type or "<type>"
        out({"count": len(res), "results": res,
             "next": [f"action-resolve {t} <id> <action>", f"event {t} <id> <event>"]})
    else:
        out(res)


def cmd_warranty_sweep(a):
    """Recompute past-due warranties to EXPIRED via the transition engine on the active backend;
    each `expire` transition opens a renew-or-extend action. The `status != EXPIRED`
    filter makes a repeat sweep idempotent (already-expired warranties are skipped)."""
    from vidushi_oa import transitions
    from vidushi_oa.backends import get_backend, query as Q
    store = get_backend().store("warranties")
    now = today(); changed = []
    for doc in store.find(Q.all_(Q.cond("expiry", "lt", now), Q.cond("status", "ne", "EXPIRED"))):
        tr = transitions.find_transition("warranties", doc.get("status"), "expire")
        if tr is None:
            continue
        if not a.dry_run:
            _apply_transition(store, doc, tr)
        changed.append(doc["id"])
    out({"expired": changed, "count": len(changed), "dry_run": bool(a.dry_run)})


def cmd_due_sweep(a):
    """Mark recurring-store docs (subscriptions, insurance, ...) DUE when their
    renewal trigger — EITHER `renews` OR `expiry` — falls within the 30-day
    lookahead, via the transition engine on the active backend; each `renewal-window`
    transition opens the domain action (e.g. cancel-before-charge for
    subscriptions, renew-policy for insurance, which carries `expiry` not
    `renews`). Recurring stores are discovered dynamically as those that declare a
    `renewal-window` transition. The `status != DUE` filter makes a repeat sweep
    idempotent (already-due docs are skipped)."""
    from vidushi_oa import transitions
    from vidushi_oa.backends import get_backend, query as Q
    cutoff = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()
    recurring = [t for t in STORES if transitions.find_transition(t, "IN_PROGRESS", "renewal-window")]
    due = {}
    count = 0
    for t in recurring:
        store = get_backend().store(t)
        ids = []
        for doc in store.find(Q.all_(
                Q.any_(Q.cond("renews", "lte", cutoff), Q.cond("expiry", "lte", cutoff)),
                Q.cond("status", "ne", "DUE"))):
            tr = transitions.find_transition(t, doc.get("status"), "renewal-window")
            if tr is None:
                continue
            if not a.dry_run:
                _apply_transition(store, doc, tr)
            ids.append(doc["id"])
        due[t] = ids
        count += len(ids)
    out({"due": due, "count": count, "dry_run": bool(a.dry_run)})


def cmd_delivery_sweep(a):
    """Open a `stuck-chase` action on every in-flight order (status NEW/UNKNOWN/IN_PROGRESS)
    that has STALLED — `last_event_date` more than 7 days ago OR a past `eta` (< today) —
    so `attention` surfaces it. Unlike warranty/due-sweep the status is NOT the idempotency
    key (a stuck order stays IN_PROGRESS); instead the query excludes orders already carrying
    an OPEN `stuck-chase`, so a repeat sweep opens none. `--dry-run` writes nothing."""
    from vidushi_oa.backends import get_backend, query as Q
    now = today()
    cutoff = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
    store = get_backend().store("orders")
    query = Q.all_(
        Q.cond("status", "in", ["NEW", "UNKNOWN", "IN_PROGRESS"]),
        Q.any_(Q.cond("last_event_date", "lt", cutoff), Q.cond("eta", "lt", now)),
        Q.none_(Q.elem("actions", Q.cond("action", "eq", "stuck-chase"), Q.cond("status", "eq", "OPEN"))),
    )
    chased = []
    for doc in store.find(query):
        if not a.dry_run:
            store.update(
                Q.cond("id", "eq", doc["id"]),
                Q.Update(set={"updated": now},
                         push={"actions": [{"action": "stuck-chase", "status": "OPEN",
                                            "owner": "user", "opened": now}]}))
        chased.append(doc["id"])
    out({"chased": chased, "count": len(chased), "dry_run": bool(a.dry_run)})


def _apply_transition(store, doc, tr):
    """Apply one declarative transition to a doc via the neutral Store: set status->`tr["to"]`
    (+ updated), fire the transition's effects (open-action / require-doc pushes,
    resolve-action flips OPEN->RESOLVED). Shared by `event` and `warranty-sweep`."""
    from vidushi_oa.backends import query as Q
    now = today()
    set_fields = {"status": tr["to"], "updated": now}
    pushes = []
    resolves = []
    for effect in tr.get("effects", []):
        op = effect.get("op")
        if op == "open-action":
            slug = effect.get("action")
            by_disp = effect.get("by_disposition")
            if by_disp:
                disp = str(doc.get("disposition") or "").upper()
                slug = by_disp.get(disp, slug)
            act = {"action": slug, "status": "OPEN", "opened": now}
            if effect.get("owner"):  act["owner"] = effect["owner"]
            if effect.get("detail"): act["detail"] = effect["detail"]
            pushes.append(act)
        elif op == "require-doc":
            pushes.append({"action": "archive-doc", "status": "OPEN", "opened": now,
                           "detail": f"archive {effect.get('type')} document"})
        elif op == "set-stage":
            set_fields["stage"] = effect.get("stage")
        elif op == "resolve-action":
            resolves.append(effect.get("action"))
    store.update(Q.cond("id", "eq", doc["id"]),
                 Q.Update(set=set_fields, push={"actions": pushes} if pushes else {}))
    for slug_name in resolves:
        store.update(
            Q.cond("id", "eq", doc["id"]),
            Q.Update(resolve=("actions",
                              (Q.cond("action", "eq", slug_name), Q.cond("status", "eq", "OPEN")),
                              {"status": "RESOLVED", "resolved": now})))


def cmd_event(a):
    """Drive a doc through the declarative transition table: look up (status, event),
    apply the matching transition (set status + fire effects), reject an unmatched
    (from, event) pair leaving the stored doc untouched."""
    from vidushi_oa import transitions
    from vidushi_oa.backends import get_backend, query as Q
    store = get_backend().store(a.type)
    doc = store.find_one(Q.cond("id", "eq", a.id))
    if doc is None:
        out({"error": "not found", "id": a.id}); sys.exit(1)
    tr = transitions.find_transition(a.type, doc.get("status"), a.event)
    if tr is None:
        out({"error": "illegal transition", "id": a.id,
             "from": doc.get("status"), "event": a.event}); sys.exit(1)
    _apply_transition(store, doc, tr)
    out({"id": a.id, "event": a.event, "from": tr["from"], "to": tr["to"]})


def _load_schema(t):
    """Load the JSON Schema for store type `t` from the packaged `vidushi_oa/schema/`."""
    from importlib.resources import files
    text = files("vidushi_oa").joinpath(f"schema/{t}.schema.json").read_text(encoding="utf-8")
    return json.loads(text)


def _apply_validators():
    """Provision each store's collection + `$jsonSchema` validator via the active backend."""
    from vidushi_oa.backends import get_backend
    return get_backend().provision({t: _load_schema(t) for t in STORES})


def cmd_apply_validators(a):
    """Attach each store's `$jsonSchema` validator to its collection (idempotent)."""
    from vidushi_oa.backends import get_backend
    done = _apply_validators()
    out({"validated": done, "db": get_backend().db_name()})


def _nonconforming_ids(t):
    """Ids of documents in collection `t` that do NOT match the store's $jsonSchema."""
    from vidushi_oa.backends import get_backend
    return get_backend().store(t).nonconforming(_load_schema(t))


def cmd_validate(a):
    """List ids of non-conforming documents. With a <type>, print a bare id array for
    that collection; with none, print a {type: [ids], ...} object across all STORES."""
    if a.type:
        out(_nonconforming_ids(a.type))
    else:
        out({t: _nonconforming_ids(t) for t in STORES})


def cmd_import(a):
    """Read each store's JSONL from DATA (honouring VIDUSHI_DATA_DIR) and upsert every
    record into Mongo by `id` (idempotent — re-running creates no duplicates)."""
    from vidushi_oa.backends import get_backend
    types = [a.type] if a.type else list(STORES)
    imported = {}
    for t in types:
        store = get_backend().store(t)
        n = 0
        for rec in load(t):
            store.replace(rec["id"], rec)
            n += 1
        imported[t] = n
    out({"imported": imported})


def cmd_init(a):
    """Create each store's collection + a unique index on `id`, then attach the
    `$jsonSchema` validators (idempotent)."""
    from vidushi_oa.backends import get_backend
    backend = get_backend()
    done = []
    for t in STORES:
        backend.store(t).ensure_id_index()
        done.append(t)
    _apply_validators()
    out({"initialized": done, "db": backend.db_name()})


def cmd_setup(a):
    """Probe the active backend's readiness (fails fast with actionable guidance if not
    ready); with --check that is all. Otherwise run the `init` provisioning (collections +
    unique `id` indexes + `$jsonSchema` validators)."""
    from vidushi_oa.backends import get_backend

    ok, message = get_backend().check()
    if not ok:
        print(message, file=sys.stderr)
        sys.exit(1)
    print(message)
    if getattr(a, "check", False):
        return
    cmd_init(a)


def cmd_snapshot(a):
    """Export each store's Mongo collection back to its JSONL file under DATA
    (honouring VIDUSHI_DATA_DIR). One JSON object per line, `_id` stripped, keys ordered
    `id` first then the rest sorted -> byte-identical output across repeated runs.
    Writes atomically (tmp file + os.replace)."""
    from vidushi_oa.backends import get_backend, query as Q
    types = [a.type] if a.type else list(STORES)
    counts = {}
    for t in types:
        docs = get_backend().store(t).find(Q.ALL)
        target = path(t)
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for d in docs:
                ordered = {"id": d.get("id"), **{k: d[k] for k in sorted(d) if k != "id"}}
                f.write(json.dumps(ordered, ensure_ascii=False) + "\n")
        os.replace(tmp, target)
        counts[t] = len(docs)
    out({"snapshot": counts})


_MAIL_PROVIDERS = ("gmail", "yahoo", "fastmail")


def _mail_row(msg):
    """Project a `Message` to the AXI mail row: id/source_tag/subject/sender/date."""
    return {"id": msg.id, "source_tag": msg.source_tag, "subject": msg.subject,
            "sender": msg.sender, "date": msg.date}


def _mail_client_or_exit():
    """Build the mail client for a mail-* verb, rendering an unresolvable
    `secret_ref` (`LookupError` from secret resolution) as a structured error +
    exit 1 (no traceback). Scoped to the mail verbs on purpose: a `LookupError`
    from any other command handler still surfaces as a real traceback at its
    fault site rather than being masked as a cryptic error payload."""
    try:
        return build_client()
    except LookupError as e:
        out({"error": str(e)})
        sys.exit(1)


def cmd_mail_search(a):
    """Server-side search across the configured accounts, merged + de-duped by
    `Message-ID` (by the client), field-projected, TOON-enveloped (`--json` -> a
    bare array with no tally/next)."""
    client = _mail_client_or_exit()
    msgs = client.search(a.query, accounts=getattr(a, "accounts", None))
    rows = [_mail_row(m) for m in msgs]
    if _FMT == "json":
        out(rows)
        return
    tally = {}
    for r in rows:
        tag = r["source_tag"].strip("[]")   # "[GM]" -> "GM" (bracket-free TOON map key)
        tally[tag] = tally.get(tag, 0) + 1
    nxt = [f"mail-search {a.query} --accounts <name>", "mail-accounts"]
    out({"count": len(rows), "tally": {"source_tag": tally}, "results": rows, "next": nxt})


def cmd_mail_accounts(a):
    """List the configured accounts + their adapter capabilities."""
    rows = [{"account": name, "capabilities": sorted(caps)}
            for name, caps in _mail_client_or_exit().accounts()]
    if _FMT == "json":
        out(rows)
    else:
        out({"results": rows, "next": ["mail-search <query>"]})


def cmd_mail_get(a):
    """Fetch one message by `--account` + `--uid` via that account's adapter. An
    unknown account or uid — or an adapter that cannot fetch by uid (JMAP) — is a
    structured error + exit 1 (no traceback), across every real adapter contract:
    `ImapAdapter` returns None for an unknown uid, `JmapAdapter` raises
    `NotImplementedError`."""
    client = _mail_client_or_exit()
    adapter = client._adapters.get(a.account)
    if adapter is None:
        out({"error": "unknown account", "account": a.account, "uid": a.uid})
        sys.exit(1)
    try:
        msg = adapter.fetch_message(a.uid)
    except KeyError:
        msg = None
    except NotImplementedError:
        out({"error": "mail-get is not supported for this account",
             "account": a.account, "uid": a.uid})
        sys.exit(1)
    if msg is None:
        out({"error": "message not found", "account": a.account, "uid": a.uid})
        sys.exit(1)
    row = _mail_row(msg)
    if _FMT == "json":
        out(row)
    else:
        out({"result": row, "next": [f"mail-search --accounts {a.account}"]})


def cmd_mail_auth(a):
    """Register a credential *reference* (provider/address/secret-ref) — never the
    secret itself. Rejects an unsupported provider with a structured error.

    Two modes: with ``--secret-ref`` the caller supplies the reference directly
    (§S5). Without it, the raw secret is obtained WITHOUT touching argv — a hidden
    prompt when interactive, else one line of stdin (the non-interactive/CI escape) —
    stored through a ``SecretResolver`` under a DERIVED reference
    ``vidushi-oa/{provider}:{address}``, and only that reference is persisted.

    ``--auth-mode xoauth2`` (Gmail only) records that the secret is a JSON blob
    ``{client_id, client_secret, refresh_token}`` driving the XOAUTH2 refresh-token
    flow; it too is entered via the hidden prompt / stdin, never as a CLI arg."""
    from vidushi_oa.mail import accounts
    from vidushi_oa.mail.secrets import SecretResolver, BACKEND_ENV
    if a.provider not in _MAIL_PROVIDERS:
        out({"error": "unsupported provider", "provider": a.provider,
             "supported": list(_MAIL_PROVIDERS)})
        sys.exit(1)
    name = f"{a.provider}:{a.address}"

    auth_mode = getattr(a, "auth_mode", "password")
    if auth_mode == "xoauth2" and a.provider != "gmail":
        out({"error": "xoauth2 auth-mode is supported for the gmail provider only",
             "provider": a.provider})
        sys.exit(1)
    if a.secret_ref:
        secret_ref = a.secret_ref
        accounts.add_account(name, a.provider, a.address, secret_ref,
                             auth_mode=auth_mode)
    else:
        if sys.stdin.isatty():
            secret = getpass.getpass(f"Secret for {name}: ")
        else:
            secret = sys.stdin.readline().rstrip("\n")
        secret_ref = f"vidushi-oa/{a.provider}:{a.address}"
        resolver = SecretResolver()
        primary = resolver._primary_backend()
        resolver.store(secret_ref, secret)
        # §S4 fallback warning: no vault was provisioned, so the secret landed in
        # the OS keyring (or the last-resort file) rather than 1Password/Bitwarden.
        if not os.environ.get(BACKEND_ENV) and primary.name not in ("1password", "bitwarden"):
            sys.stderr.write(
                f"vidushi-oa: no vault (1Password/Bitwarden) provisioned; "
                f"stored the secret in the '{primary.name}' backend instead.\n")
        accounts.add_account(name=name, provider=a.provider, address=a.address,
                             secret_ref=secret_ref, auth_mode=auth_mode)

    out({"status": "registered", "name": name, "provider": a.provider,
         "address": a.address, "secret_ref": secret_ref, "auth_mode": auth_mode,
         "source_tag": SOURCE_TAGS[a.provider]})


def cmd_doctor(a):
    """Diagnostic health read (absorbs ``setup --check``): engine version, the active
    STORE backend + its reachability, the active SECRET backend, and one row per
    configured account reporting whether its reference resolves (plus a fix hint when
    it does not). Never prints a secret value. Exits non-zero when the store is
    unreachable or any account fails to resolve — after emitting the payload."""
    from vidushi_oa.backends import get_backend
    from vidushi_oa.mail import accounts
    from vidushi_oa.mail.secrets import SecretResolver
    from vidushi_oa import __version__

    backend = get_backend()
    store_ok, _msg = backend.check()

    resolver = SecretResolver()
    secret_backend = resolver._primary_backend().name

    rows = []
    all_resolve = True
    for entry in accounts.load_accounts():
        ref = entry.get("secret_ref", "")
        try:
            resolver.resolve(ref)
            resolves = True
        except LookupError:
            resolves = False
        if not resolves:
            all_resolve = False
        kind = "1password" if ref.startswith("op://") else secret_backend
        hint = "" if resolves else (
            f"secret_ref {ref} did not resolve; re-run "
            f"`voa mail-auth --provider {entry.get('provider')} "
            f"--address {entry.get('address')}` to store it")
        rows.append({"account": entry.get("name"), "provider": entry.get("provider"),
                     "auth_mode": entry.get("auth_mode", "password"),
                     "kind": kind, "resolves": resolves, "hint": hint})

    out({"engine": __version__,
         "store_backend": {"name": backend.name, "ok": bool(store_ok)},
         "secret_backend": secret_backend,
         "accounts": rows})

    if not store_ok or not all_resolve:
        sys.exit(1)


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
    st = add_parser("stats"); with_type(st); read_json(st); st.add_argument("--by"); st.set_defaults(func=cmd_stats)

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
    at = add_parser("attention"); at.add_argument("type", nargs="?", choices=STORES.keys()); read_json(at)
    at.set_defaults(func=cmd_attention)
    ws = add_parser("warranty-sweep"); ws.add_argument("--dry-run", action="store_true", dest="dry_run")
    ws.set_defaults(func=cmd_warranty_sweep)
    ds = add_parser("due-sweep"); ds.add_argument("--dry-run", action="store_true", dest="dry_run")
    ds.set_defaults(func=cmd_due_sweep)
    dl = add_parser("delivery-sweep"); dl.add_argument("--dry-run", action="store_true", dest="dry_run")
    dl.set_defaults(func=cmd_delivery_sweep)
    ev = add_parser("event"); with_type(ev); ev.add_argument("id"); ev.add_argument("event")
    ev.set_defaults(func=cmd_event)
    va = add_parser("validate"); va.add_argument("type", nargs="?", choices=STORES.keys()); read_json(va)
    va.set_defaults(func=cmd_validate)
    im = add_parser("import"); im.add_argument("type", nargs="?", choices=STORES.keys())
    im.set_defaults(func=cmd_import)
    sn = add_parser("snapshot"); sn.add_argument("type", nargs="?", choices=STORES.keys())
    sn.set_defaults(func=cmd_snapshot)
    it = add_parser("init"); it.set_defaults(func=cmd_init)
    su = add_parser("setup"); su.add_argument("--check", action="store_true", dest="check")
    su.set_defaults(func=cmd_setup)
    av = add_parser("apply-validators"); av.set_defaults(func=cmd_apply_validators)

    # embedded mail client (CR-OA-020 §S5) — reference-only auth + read verbs
    msr = add_parser("mail-search"); msr.add_argument("query")
    msr.add_argument("--accounts", type=lambda s: s.split(",") if s else None,
                     help="comma-separated account names to search (default: all)")
    read_json(msr); read_full(msr); msr.set_defaults(func=cmd_mail_search)
    mac = add_parser("mail-accounts"); read_json(mac); mac.set_defaults(func=cmd_mail_accounts)
    mge = add_parser("mail-get"); mge.add_argument("--account", required=True)
    mge.add_argument("--uid", required=True); read_json(mge); mge.set_defaults(func=cmd_mail_get)
    mau = add_parser("mail-auth"); mau.add_argument("--provider", required=True)
    mau.add_argument("--address", required=True)
    mau.add_argument("--auth-mode", dest="auth_mode",
                     choices=["password", "xoauth2"], default="password",
                     help="gmail only: 'xoauth2' expects the secret to be a JSON blob "
                          "{client_id, client_secret, refresh_token}; default 'password'.")
    mau.add_argument("--secret-ref", dest="secret_ref", default=None,
                     help="credential reference (op://…/keyring/file). Omit to be prompted "
                          "(hidden) or to pipe the secret on stdin; it is stored under a "
                          "derived reference and never accepted as a CLI arg.")
    read_json(mau); mau.set_defaults(func=cmd_mail_auth)
    dr = add_parser("doctor"); read_json(dr); dr.set_defaults(func=cmd_doctor)

    # Content-first no-arg path (S6/AXI #8): a truly-empty argv (just the
    # program name) prints live data — a one-line description + the executable
    # path token, then the `attention` worklist — instead of letting argparse
    # error with `usage:`. `-h`/`--help` still reaches argparse (handled there).
    global _FMT
    if len(sys.argv) == 1:
        env = os.environ.get("VIDUSHI_FORMAT")
        _FMT = env if env in ("toon", "json") else "toon"
        print("vidushi-oa store (scripts/store.py) — local "
              "personal-admin data CLI. Rows needing attention:")
        cmd_attention(argparse.Namespace(type=None))
        return

    a = p.parse_args()
    fmt_choice = getattr(a, "format", None)          # explicit flag wins
    if fmt_choice is None:
        if getattr(a, "json_out", False):            # --json read shortcut
            fmt_choice = "json"
        else:
            env = os.environ.get("VIDUSHI_FORMAT")
            fmt_choice = env if env in ("toon", "json") else "toon"   # env, else default; garbage env -> toon
    _FMT = fmt_choice
    try:
        a.func(a)
    except ValueError as e:            # unknown VIDUSHI_BACKEND from get_backend()
        out({"error": str(e)}); sys.exit(1)
    except NotImplementedError as e:   # e.g. sqlite --filter (mongo-only)
        out({"error": str(e)}); sys.exit(1)


if __name__ == "__main__":
    main()
