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
import argparse, json, os, sys, datetime, re, getpass, imaplib, urllib.error
import email.utils

# Module-level seam: tests monkeypatch `vidushi_oa._cli.build_client`; the
# `cmd_mail_*` handlers call it (no args) to obtain a wired `MailClient`.
from vidushi_oa.mail.base import SOURCE_TAGS
from vidushi_oa.mail.factory import build_client
from vidushi_oa.mail import accounts, send_gate
from vidushi_oa.mail import compose as compose_mod
from vidushi_oa.mail.compose import compose
from vidushi_oa.mail.schema_org import extract_schema_org
from vidushi_oa.mail.extract_map import to_store_candidates

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
    unique `id` indexes + `$jsonSchema` validators).

    When ``--secret-backend`` is passed it instead runs the CR-OA-023 §S3 OS-aware
    secret-provisioning + pre-flight path (a distinct concern from STORE provisioning)
    and returns; a bare ``setup`` keeps the existing STORE ``check()``/``init`` behavior."""
    secret_backend = a.secret_backend
    if secret_backend:
        _setup_secret_backend(secret_backend)
        return

    from vidushi_oa.backends import get_backend

    ok, message = get_backend().check()
    if not ok:
        print(message, file=sys.stderr)
        sys.exit(1)
    print(message)
    if getattr(a, "check", False):
        return
    cmd_init(a)


def _setup_secret_backend(choice):
    """OS-aware secret-backend provisioning + pre-flight (CR-OA-023 §S3).

    ``choice`` is one of ``auto``/``keyring``/``file``:
      - ``auto`` selects the OS keyring when a Secret-Service provider is reachable
        (proven by the pre-flight round-trip); otherwise it emits OS-specific guidance
        naming the provider and reports the gap WITHOUT silently writing a file secret.
      - ``file`` is the explicit, confirmed file-backend choice.
      - ``keyring`` forces the keyring and reports pre-flight success/failure.

    The structured status carries ``secret_backend``, ``confirmed`` (an explicit
    stated choice), and a ``preflight`` report ``{ok: bool}``; exit is non-zero when
    no usable backend was provisioned."""
    from vidushi_oa.mail.secrets import (
        FileBackend, KeyringBackend, detect_desktop, keyring_guidance, preflight,
    )

    desktop = detect_desktop()

    if choice == "file":
        pf = preflight(FileBackend())
        out({"secret_backend": "file", "confirmed": True,
             "desktop": desktop, "preflight": pf})
        sys.exit(0 if pf.get("ok") else 1)

    if choice == "keyring":
        pf = preflight(KeyringBackend())
        out({"secret_backend": "keyring", "confirmed": True,
             "desktop": desktop, "preflight": pf})
        sys.exit(0 if pf.get("ok") else 1)

    # choice == "auto": prove the keyring is reachable before selecting it.
    pf = preflight(KeyringBackend())
    if pf.get("ok"):
        out({"secret_backend": "keyring", "confirmed": False,
             "desktop": desktop, "preflight": pf})
        return

    # No reachable provider: name the OS-specific remedy + the explicit file escape
    # hatch, and report the gap — never a silent downgrade to the file backend.
    guidance = keyring_guidance(desktop)
    sys.stderr.write("vidushi-oa: " + guidance + "\n")
    out({"error": guidance, "desktop": desktop, "preflight": pf,
         "next": ["setup --secret-backend file"]})
    sys.exit(1)


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
    """Project a `Message` to the AXI mail row: id/uid/account/source_tag/subject/sender/date."""
    return {"id": msg.id, "uid": msg.uid, "account": msg.account,
            "source_tag": msg.source_tag, "subject": msg.subject,
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
    bare array with no tally/next). Fail-soft: one bad account (revoked token, down
    host) is surfaced in `failed_accounts` alongside the healthy results (exit 0);
    only a total wipeout — every selected account failed — is a structured error +
    exit 1 (no traceback), per AXI #6."""
    client = _mail_client_or_exit()
    msgs = client.search(a.query, accounts=getattr(a, "accounts", None))
    failures = client.last_failures
    if failures and client.last_succeeded == 0:
        out({"error": "all selected accounts failed", "failed_accounts": failures})
        sys.exit(1)
    rows = [_mail_row(m) for m in msgs]
    if _FMT == "json":
        if failures:
            detail = ", ".join(
                f"{f['account']} ({f['error']})" for f in failures)
            sys.stderr.write(
                f"warn: mail-search: {len(failures)} account(s) failed: {detail}\n")
        out(rows)
        return
    tally = {}
    for r in rows:
        tag = r["source_tag"].strip("[]")   # "[GM]" -> "GM" (bracket-free TOON map key)
        tally[tag] = tally.get(tag, 0) + 1
    nxt = [f"mail-search {a.query} --accounts <name>", "mail-accounts"]
    if rows:
        first = rows[0]
        nxt.insert(0, f"mail-get --account {first['account']} --uid {first['uid']}")
    envelope = {"count": len(rows), "tally": {"source_tag": tally}, "results": rows}
    if failures:
        envelope["failed_accounts"] = failures
    envelope["next"] = nxt
    out(envelope)


def cmd_mail_accounts(a):
    """List the configured accounts + their adapter capabilities. Fail-soft: an
    account whose `secret_ref` cannot be resolved is skipped from the listing and
    surfaced in `failed_accounts` (TOON) / a stderr warning (`--json`), never a
    traceback — the healthy accounts still list. Only a total wipeout — every
    configured account failed to build — is a structured error + exit 1 (no
    traceback), symmetric with `mail-search`."""
    client = _mail_client_or_exit()
    rows = [{"account": name, "capabilities": sorted(caps)}
            for name, caps in client.accounts()]
    failures = client.build_failures
    if failures and not rows:
        out({"error": "all configured accounts failed", "failed_accounts": failures})
        sys.exit(1)
    if _FMT == "json":
        if failures:
            detail = ", ".join(
                f"{f['account']} ({f['error']})" for f in failures)
            sys.stderr.write(
                f"warn: mail-accounts: {len(failures)} account(s) failed: {detail}\n")
        out(rows)
    else:
        envelope = {"results": rows}
        if failures:
            envelope["failed_accounts"] = failures
        envelope["next"] = ["mail-search <query>"]
        out(envelope)


# Every exception a real mail adapter raises on a LIVE failure: a JMAP non-200 or
# method-level rejection (`RuntimeError`), a missing key/uid (`LookupError`), the
# IMAP/network surface (`imaplib.IMAP4.error` / `OSError`, which `urllib.error.URLError`
# subclasses), and a 2xx whose body the JMAP transport cannot parse — a captive-portal
# or proxy interception page makes its `json.loads` raise `json.JSONDecodeError` or
# `UnicodeDecodeError`, both of which `ValueError` already covers. Rendered
# structurally by `_mail_failure_exit` — never as a traceback.
_MAIL_LIVE_ERRORS = (LookupError, RuntimeError, imaplib.IMAP4.error, OSError,
                     urllib.error.URLError, ValueError)


def _mail_failure_exit(e, **context):
    """Render a live mail-adapter failure as the structured `{"error", ...}` payload
    + exit 1 (AXI #6: no traceback) — the shared seam every mail verb uses."""
    out({"error": str(e) or e.__class__.__name__, **context})
    sys.exit(1)


def cmd_mail_get(a):
    """Fetch one message by `--account` + `--uid` via that account's adapter. An
    unknown account or uid — or an adapter that cannot fetch by uid (JMAP) — is a
    structured error + exit 1 (no traceback), across every real adapter contract:
    `ImapAdapter` returns None for an unknown uid, `JmapAdapter` raises
    `NotImplementedError`. A live fetch/connect failure (down host, DNS failure,
    bad app-password, or a network-down XOAUTH2 refresh — `imaplib.IMAP4.error` /
    `OSError` / `urllib.error.URLError`) is rendered structurally too, never as a
    raw traceback."""
    client = _mail_client_or_exit()
    adapter = client._adapters.get(a.account)
    if adapter is None:
        build_failure = next(
            (f for f in client.build_failures if f["account"] == a.account), None)
        error = build_failure["error"] if build_failure else "unknown account"
        out({"error": error, "account": a.account, "uid": a.uid})
        sys.exit(1)
    try:
        msg = adapter.fetch_message(a.uid)
    except KeyError:
        msg = None
    except NotImplementedError:
        out({"error": "mail-get is not supported for this account",
             "account": a.account, "uid": a.uid})
        sys.exit(1)
    except _MAIL_LIVE_ERRORS as e:
        _mail_failure_exit(e, account=a.account, uid=a.uid)
    if msg is None:
        out({"error": "message not found", "account": a.account, "uid": a.uid})
        sys.exit(1)
    row = _mail_row(msg)
    if _FMT == "json":
        out(row)
    else:
        out({"result": row, "next": [f"mail-search --accounts {a.account}"]})


def cmd_mail_extract(a):
    """Extract structured store candidates from one message's HTML body (§S4/§S5).

    Fetches the message body via the account's adapter (`fetch_html_body`, §S1),
    parses schema.org JSON-LD/microdata into entities (`extract_schema_org`, §S2),
    and maps them onto `{"type", "candidate"}` store candidate rows
    (`to_store_candidates`, §S3), returned as an AXI TOON envelope
    `{count, results, next}` (`--json` -> a bare candidates array).

    Read-only: NO autonomous store write — `next[]` only *suggests* the exact
    `voa add <type> --json '<candidate>'` for the agent to run. No schema.org
    markup -> the definitive empty state (`count: 0`, exit 0, NOT an error) so the
    skill falls back to heuristic extraction. The raw HTML body is never surfaced.
    An unknown account is a structured error + exit 1 (no traceback), same seam as
    `cmd_mail_get`."""
    client = _mail_client_or_exit()
    adapter = _mail_adapter_or_exit(client, a.account, uid=a.uid)
    try:
        html = adapter.fetch_html_body(a.uid)
    except NotImplementedError:
        out({"error": "mail-extract is not supported for this account",
             "account": a.account, "uid": a.uid})
        sys.exit(1)
    except _MAIL_LIVE_ERRORS as e:
        _mail_failure_exit(e, account=a.account, uid=a.uid)
    entities = extract_schema_org(html or "")
    candidates = to_store_candidates(entities)
    if _FMT == "json":
        out(candidates)
        return
    if candidates:
        first = candidates[0]
        nxt = [f"voa add {first['type']} --json '{json.dumps(first['candidate'])}'"]
        if len(candidates) > 1:
            nxt.append("review the remaining candidates before adding them")
    else:
        nxt = ["no schema.org markup found — fall back to heuristic extraction"]
    out({"count": len(candidates), "results": candidates, "next": nxt})


def _mail_adapter_or_exit(client, account, **extra):
    """Resolve `account`'s adapter via the same `client._adapters` seam `cmd_mail_get`
    uses, or render an unknown-account structured error + exit 1 (no traceback).

    Shared by every account-scoped verb — the draft-then-confirm trio
    (`mail-draft`/`mail-send`/`mail-reply`) AND read-only `mail-extract` — so it
    resolves an account and nothing more: send-gating belongs in the send verbs,
    not here, where it would also gate a read."""
    adapter = client._adapters.get(account)
    if adapter is None:
        build_failure = next(
            (f for f in client.build_failures if f["account"] == account), None)
        error = build_failure["error"] if build_failure else "unknown account"
        out({"error": error, "account": account, **extra})
        sys.exit(1)
    return adapter


def _recipient_addresses(value):
    """Every bare address carried by a recipient header value (`To`/`Cc`), which may
    hold a comma-separated list and display names. Falls back to the raw value when
    nothing parses, so an unparsable recipient is still checked (and refused) rather
    than silently skipped."""
    if not value:
        return []
    addresses = [addr for _name, addr in email.utils.getaddresses([value]) if addr]
    return addresses or [str(value).strip()]


def _verified_recipient_or_exit(recipient, force, **extra):
    """Verified-recipient guard (§S4): EVERY address in the outbound `recipient`
    header value must match a `contact`'s `support_email` (the verified-address
    allow-list) unless `force` is set. A non-matching address is a structured error
    naming it + exit 1 (no traceback); `--force` bypasses the check entirely.

    The value is parsed rather than compared whole because a single `To`/`Cc` carries
    a LIST: checking only the raw string would let every address after the first
    reach the transport unverified (`send_draft` builds its RCPT list from all of
    `To` + `Cc`)."""
    if force:
        return
    from vidushi_oa.backends import get_backend, query as Q
    store = get_backend().store("contacts")
    for address in _recipient_addresses(recipient):
        match = store.find_one(Q.cond("support_email", "eq", address))
        if match is None:
            out({"error": f"recipient {address} is not a verified contact "
                          f"(no matching support_email); pass --force to override",
                 **extra})
            sys.exit(1)


def _validate_from_or_exit(account, from_addr, **extra):
    """From-identity guard (§S4): `from_addr` must be one of the account's own
    identities — its registered `address` plus any configured `aliases`. Delegates
    to `compose.validate_from`; an unknown From is a structured error naming it +
    exit 1. Skipped when the account is not in the registry (nothing to validate
    against)."""
    entry = next((e for e in accounts.load_accounts()
                  if e.get("name") == account), None)
    if entry is None:
        return
    identities = {entry.get("address")} | set(entry.get("aliases", []))
    try:
        compose_mod.validate_from(from_addr, identities)
    except ValueError as e:
        out({"error": str(e), "account": account, **extra})
        sys.exit(1)


# §S5 — the FK flags a draft can carry (flag name -> the STORE TYPE it targets) and,
# per store type, the OPEN correspondence action a linked send resolves.
_MAIL_FK_STORES = {"case": "cases", "invoice": "invoices",
                   "warranty": "warranties", "order": "orders"}
_CORRESPONDENCE_ACTION = {"cases": "raise-ticket"}


def _drafted_status(account, draft_id):
    """The flat status a saved draft emits, carrying the AXI #9 `next[]` whose single
    entry is the runnable confirm-and-send step for THIS draft (`mail-send --account
    <a> --draft <id>`). The hint is TOON-only — `--json` stays the bare object the
    CR-OA-010 decision-B contract pins."""
    status = {"status": "drafted", "draft": draft_id, "account": account}
    if _FMT == "toon":
        status["next"] = [f"mail-send --account {account} --draft {draft_id}"]
    return status


def _save_draft_link(a, draft_id):
    """If the draft carried an FK flag (`--case`/`--invoice`/…), persist a
    draft_id -> (store type, row id) link so `mail-send` can record the sent message
    on that row. Only the FIRST supplied FK is linked."""
    from vidushi_oa.mail import draft_links
    for flag, store_type in _MAIL_FK_STORES.items():
        fk_id = getattr(a, flag, None)
        if fk_id:
            draft_links.save_link(draft_id, store_type, fk_id)
            return


def _attachments_or_exit(path):
    """Read the `--attach` file at *path* and return the ``[(basename, bytes)]``
    list `compose(..., attachments=...)` expects, or `None` when no attachment was
    requested. A missing/unreadable file is a structured error + exit 1 (no
    traceback), consistent with the other mail verbs."""
    if not path:
        return None
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as e:
        out({"error": "attachment not readable", "path": path, "reason": str(e)})
        sys.exit(1)
    return [(os.path.basename(path), data)]


def cmd_mail_draft(a):
    """Compose (§S2) and save a REAL draft via the account adapter's
    `create_draft(raw)`; emit a flat TOON/JSON status carrying the `draft` id (and,
    in TOON, the AXI #9 `next[]` holding the runnable `mail-send` for that draft).
    Performs ZERO network send — draft-then-confirm requires `mail-send` be the only
    code path that can dispatch a message."""
    client = _mail_client_or_exit()
    adapter = _mail_adapter_or_exit(client, a.account)
    force = getattr(a, "force", False)
    _verified_recipient_or_exit(a.to, force, account=a.account, to=a.to)
    _verified_recipient_or_exit(a.cc, force, account=a.account, cc=a.cc)
    _validate_from_or_exit(a.account, a.from_addr, to=a.to)
    attachments = _attachments_or_exit(getattr(a, "attach", None))
    if attachments is None:
        raw = compose(a.from_addr, a.to, a.subject, a.body, cc=a.cc)
    else:
        raw = compose(a.from_addr, a.to, a.subject, a.body, cc=a.cc,
                      attachments=attachments)
    try:
        draft_id = adapter.create_draft(raw)
    except _MAIL_LIVE_ERRORS as e:
        _mail_failure_exit(e, account=a.account, to=a.to)
    _save_draft_link(a, draft_id)
    out(_drafted_status(a.account, draft_id))


def cmd_mail_send(a):
    """Dispatch ONLY the identified draft via the adapter's `send_draft(draft_id)`,
    gated on `send_gate.ensure_send_capable(entry)` (a non-send-capable account is a
    structured error + exit 1 whose message names "send"). Emits the sent
    `message_id`, plus — for a draft that was FK-linked (§S5), in TOON only — an AXI #9
    `next[]` pointing at the row the correspondence was recorded on (`get <type> <id>`);
    an unlinked send has no follow-up step, so it omits `next`. This is the ONLY
    function in this module that may call a send-path token."""
    client = _mail_client_or_exit()
    entry = next((e for e in accounts.load_accounts()
                  if e.get("name") == a.account), None)
    if entry is None:
        out({"error": "unknown account", "account": a.account})
        sys.exit(1)
    try:
        send_gate.ensure_send_capable(entry)
    except PermissionError as e:
        out({"error": str(e), "account": a.account})
        sys.exit(1)
    adapter = _mail_adapter_or_exit(client, a.account, draft=a.draft)
    try:
        message_id = adapter.send_draft(a.draft)
    except _MAIL_LIVE_ERRORS as e:
        _mail_failure_exit(e, account=a.account, draft=a.draft)
    linked = _record_sent_correspondence(a.draft, message_id)
    status = {"status": "sent", "message_id": message_id,
              "draft": a.draft, "account": a.account}
    if linked:
        status["linked"] = linked
        if _FMT == "toon":
            status["next"] = [f"get {linked['type']} {linked['id']}"]
    out(status)


def _record_sent_correspondence(draft_id, message_id):
    """§S5 — if `draft_id` was linked to a store row, record the sent message as a
    `correspondence` document on that row and resolve the row's mapped OPEN
    correspondence action. Returns the linkage summary, or `None` for an unlinked
    (inert) send."""
    from vidushi_oa.mail import draft_links
    from vidushi_oa.backends import get_backend, query as Q
    link = draft_links.pop_link(draft_id)
    if link is None:
        return None
    store_type, row_id = link["fk_field"], link["fk_id"]
    store = get_backend().store(store_type)
    doc = {"type": "correspondence", "message_id": message_id}
    action = _CORRESPONDENCE_ACTION.get(store_type)
    if action:
        store.update(
            Q.cond("id", "eq", row_id),
            Q.Update(set={"updated": today()},
                     push={"documents": [doc]},
                     resolve=("actions",
                              (Q.cond("action", "eq", action),
                               Q.cond("status", "eq", "OPEN")),
                              {"status": "RESOLVED", "resolved": today()})))
    else:
        store.update(Q.cond("id", "eq", row_id),
                     Q.Update(set={"updated": today()}, push={"documents": [doc]}))
    return {"type": store_type, "id": row_id, "action": action}


def cmd_mail_reply(a):
    """Fetch the source message via the adapter's `fetch_message(uid)`, compose a
    THREADED reply (In-Reply-To + References from the fetched `Message`), and save it
    as a draft exactly like `cmd_mail_draft` — ZERO send. An unknown/missing source
    uid is a structured error + exit 1 (no traceback)."""
    client = _mail_client_or_exit()
    adapter = _mail_adapter_or_exit(client, a.account, uid=a.uid)
    try:
        source = adapter.fetch_message(a.uid)
    except KeyError:
        source = None
    except _MAIL_LIVE_ERRORS as e:
        _mail_failure_exit(e, account=a.account, uid=a.uid)
    if source is None:
        out({"error": "message not found", "account": a.account, "uid": a.uid})
        sys.exit(1)
    references = [ref for ref in (source.references, source.id) if ref]
    subject = source.subject if source.subject.lower().startswith("re:") \
        else f"Re: {source.subject}"
    to = source.sender or source.to
    _verified_recipient_or_exit(to, getattr(a, "force", False),
                                account=a.account, to=to)
    _validate_from_or_exit(a.account, a.from_addr, to=to)
    attachments = _attachments_or_exit(getattr(a, "attach", None))
    if attachments is None:
        raw = compose(a.from_addr, to, subject, a.body,
                      in_reply_to=source.id, references=references)
    else:
        raw = compose(a.from_addr, to, subject, a.body,
                      in_reply_to=source.id, references=references,
                      attachments=attachments)
    try:
        draft_id = adapter.create_draft(raw)
    except _MAIL_LIVE_ERRORS as e:
        _mail_failure_exit(e, account=a.account, to=to)
    _save_draft_link(a, draft_id)
    out(_drafted_status(a.account, draft_id))


def _read_secret_no_argv(name):
    """Obtain the raw secret WITHOUT touching argv: a hidden prompt when interactive,
    else one line of stdin (the non-interactive/CI escape). Shared by ``mail-auth``
    and ``doctor --fix`` so the hidden-input path is never duplicated (DN Decision 6)."""
    if sys.stdin.isatty():
        return getpass.getpass(f"Secret for {name}: ")
    return sys.stdin.readline().rstrip("\n")


def _provision_account_secret(provider, address, auth_mode="password", send=False,
                              aliases=None, endpoint=None):
    """Interactive secret-entry shared by ``cmd_mail_auth`` and ``doctor --fix``.

    Reads the secret via the hidden-input/stdin path ONLY (never a CLI arg), stores
    it under the derived reference ``vidushi-oa/{provider}:{address}`` through a
    ``SecretResolver``, registers/updates the account, and returns that reference.
    Only the reference — never the raw secret — is persisted in the accounts file."""
    from vidushi_oa.mail import accounts
    from vidushi_oa.mail.secrets import SecretResolver, BACKEND_ENV
    name = f"{provider}:{address}"
    secret = _read_secret_no_argv(name)
    secret_ref = f"vidushi-oa/{provider}:{address}"
    resolver = SecretResolver()
    primary = resolver._primary_backend()
    resolver.store(secret_ref, secret)
    # CR-OA-023 §S3: with no backend pinned the backend was auto-selected under the
    # keyring->file model — report which backend the secret landed in so reaching the
    # last-resort file backend is never a silent downgrade.
    if not os.environ.get(BACKEND_ENV):
        if primary.name == "file":
            sys.stderr.write(
                "vidushi-oa: no OS keyring provider was reachable; stored the secret "
                "in the last-resort 0600 'file' backend. Run 'voa setup "
                "--secret-backend auto' for OS-specific keyring guidance.\n")
        else:
            sys.stderr.write(
                f"vidushi-oa: no secret backend pinned; stored the secret in the "
                f"auto-selected '{primary.name}' backend.\n")
    accounts.add_account(name=name, provider=provider, address=address,
                         secret_ref=secret_ref, auth_mode=auth_mode, send=send,
                         aliases=aliases or [], endpoint=endpoint)
    return secret_ref


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
    flow; it too is entered via the hidden prompt / stdin, never as a CLI arg.

    RE-REGISTRATION IS ADDITIVE, NEVER DESTRUCTIVE. ``add_account`` replaces the
    matched entry wholesale, so every field the CLI does not re-specify is read off
    the existing entry first — ``send``, ``aliases`` and ``auth_mode`` alongside the
    ``endpoint`` the registry itself already carries forward. Without that, a
    re-register aimed at ONE field (rotating the secret, or clearing a
    ``tls_verify: false`` override on `doctor`'s advice) silently revoked send
    capability, wiped every configured alias, and reset an XOAUTH2 Gmail account to
    ``password`` — which then builds a plain `GmailImapAdapter`. ``--send`` is
    `store_true`, so an omitted flag cannot mean "revoke"; it only ever ADDS the
    capability, and dropping it is a deliberate edit of the registry."""
    from vidushi_oa.mail import accounts
    if a.provider not in _MAIL_PROVIDERS:
        out({"error": "unsupported provider", "provider": a.provider,
             "supported": list(_MAIL_PROVIDERS)})
        sys.exit(1)
    name = f"{a.provider}:{a.address}"
    existing = next((e for e in accounts.load_accounts()
                     if e.get("name") == name), None) or {}

    auth_mode = (getattr(a, "auth_mode", None)
                 or existing.get("auth_mode") or "password")
    if auth_mode == "xoauth2" and a.provider != "gmail":
        out({"error": "xoauth2 auth-mode is supported for the gmail provider only",
             "provider": a.provider})
        sys.exit(1)
    send = bool(getattr(a, "send", False)) or bool(existing.get("send", False))
    aliases = getattr(a, "alias", None) or list(existing.get("aliases") or [])
    endpoint_raw = getattr(a, "endpoint", None)
    # `None` (flag omitted) preserves any configured override; an explicit `{}`
    # CLEARS it — the only way to re-enable TLS verification on an account that was
    # registered with `tls_verify: false` without hand-editing the accounts file.
    if endpoint_raw is not None:
        try:
            endpoint = json.loads(endpoint_raw)
        except json.JSONDecodeError as e:
            out({"error": "invalid --endpoint JSON", "detail": str(e)})
            sys.exit(1)
        if not isinstance(endpoint, dict):
            out({"error": "--endpoint must be a JSON object"})
            sys.exit(1)
    else:
        endpoint = None
    if a.secret_ref:
        secret_ref = a.secret_ref
        accounts.add_account(name, a.provider, a.address, secret_ref,
                             auth_mode=auth_mode, send=send, aliases=aliases,
                             endpoint=endpoint)
    else:
        secret_ref = _provision_account_secret(
            a.provider, a.address, auth_mode, send, aliases, endpoint)

    out({"status": "registered", "name": name, "provider": a.provider,
         "address": a.address, "secret_ref": secret_ref, "auth_mode": auth_mode,
         "send": send, "source_tag": SOURCE_TAGS[a.provider]})


def cmd_doctor(a):
    """Diagnostic health read (absorbs ``setup --check``): engine version, the active
    STORE backend + its reachability, the active SECRET backend, and one row per
    configured account reporting whether its reference resolves (plus a fix hint when
    it does not). Never prints a secret value. Exits non-zero when the store is
    unreachable or any account fails to resolve — after emitting the payload."""
    from vidushi_oa.backends import get_backend
    from vidushi_oa.mail import accounts
    from vidushi_oa.mail.secrets import (SecretResolver, KeyringBackend, preflight,
                                         detect_desktop, keyring_guidance, BACKEND_ENV)
    from vidushi_oa import __version__

    backend = get_backend()
    store_ok, _msg = backend.check()

    resolver = SecretResolver()
    # Determine the ACTIVE secret backend by REACHABILITY, not mere importability:
    # a pinned backend is honoured as-is, otherwise the keyring backend must survive
    # a `set`->`get` round-trip (preflight) to count as reachable — else we fall
    # through to the last-resort file backend. `KeyringBackend.available()` alone only
    # checks the module import and misreports "keyring" when no provider is reachable.
    if os.environ.get(BACKEND_ENV):
        secret_backend = resolver._primary_backend().name
    else:
        secret_backend = "keyring" if preflight(KeyringBackend()).get("ok") else "file"
    # Reaching the file backend is an explicit, stated choice (never a silent
    # downgrade) — carry a confirmed marker + the OS-specific remedy hint.
    secret_backend_confirmed = secret_backend == "file"
    secret_backend_hint = (
        keyring_guidance(detect_desktop()) if secret_backend == "file" else "")

    # `--fix`: for every account whose reference does not resolve, INSTANTIATE the same
    # interactive mail-auth secret-entry (hidden-input/stdin only, never argv). Done
    # before the resolution report so a freshly-provisioned account reads as healthy.
    if a.fix:
        for entry in accounts.load_accounts():
            ref = entry.get("secret_ref", "")
            try:
                resolver.resolve(ref)
                continue
            except Exception:  # noqa: BLE001 - any resolution failure => needs re-auth
                pass
            _provision_account_secret(entry.get("provider"), entry.get("address"),
                                      entry.get("auth_mode", "password"),
                                      send=entry.get("send", False),
                                      aliases=entry.get("aliases") or [])

    rows = []
    all_resolve = True
    tls_disabled = []
    for entry in accounts.load_accounts():
        ref = entry.get("secret_ref", "")
        try:
            resolver.resolve(ref)
            resolves = True
        except Exception:  # noqa: BLE001 - any resolution failure => account unresolved
            resolves = False
        if not resolves:
            all_resolve = False
        kind = secret_backend
        hint = "" if resolves else (
            f"secret_ref {ref} did not resolve; re-run "
            f"`voa mail-auth --provider {entry.get('provider')} "
            f"--address {entry.get('address')}` to store it")
        # An endpoint override — above all its `tls_verify: false` key, which turns
        # certificate/hostname verification OFF for this account's IMAP/SMTP channels
        # — must never be invisible: without it here an account running unverified
        # TLS reads identically to a hardened one in every diagnostic.
        endpoint = entry.get("endpoint") or {}
        if not bool(endpoint.get("tls_verify", True)):
            tls_disabled.append((entry, endpoint))
        rows.append({"account": entry.get("name"), "provider": entry.get("provider"),
                     "auth_mode": entry.get("auth_mode", "password"),
                     "kind": kind, "resolves": resolves,
                     "endpoint": ", ".join(f"{k}={endpoint[k]}"
                                           for k in sorted(endpoint)),
                     "tls_verify": bool(endpoint.get("tls_verify", True)),
                     "hint": hint})

    # Ordered, machine-readable remediation plan — one step per detected gap, each
    # carrying a boolean human_input flag. Fix the backend BEFORE re-authing accounts:
    # the "enable Secret Service" step precedes the per-account "run mail-auth" steps.
    remediation = []
    next_items = []
    unresolved = [r for r in rows if not r["resolves"]]
    if secret_backend == "file" and unresolved:
        ss_step = ("Enable the OS Secret Service so the keyring backend is reachable. "
                   + secret_backend_hint)
        remediation.append({"step": ss_step, "human_input": True})
        next_items.append(ss_step)
    for r in unresolved:
        ma_step = (
            f"Run mail-auth for {r['account']}: `voa mail-auth "
            f"--provider {r['provider']} --address "
            f"{r['account'].split(':', 1)[-1]}` and enter the secret at the hidden "
            f"prompt (or pipe it on stdin), or run `voa doctor --fix`.")
        remediation.append({"step": ma_step, "human_input": True})
        next_items.append(ma_step)
    # The suggested command drops ONLY the `tls_verify` key — every other endpoint
    # key (the emulator host/port an account may legitimately be pointed at) is
    # re-sent verbatim — and passes the account's own `--secret-ref`, so the stored
    # credential is never re-read from stdin. `cmd_mail_auth` carries `send` /
    # `aliases` / `auth_mode` forward, so the step restores a verifying channel and
    # changes nothing else; that is what makes it safe to run unattended.
    for entry, endpoint in tls_disabled:
        verifying = {k: v for k, v in endpoint.items() if k != "tls_verify"}
        tls_step = (
            f"{entry.get('name')} runs with TLS certificate/hostname verification "
            f"DISABLED (endpoint tls_verify=false) — intended only for the local "
            f"emulator. Restore a verifying channel with `voa mail-auth --provider "
            f"{entry.get('provider')} --address {entry.get('address')} "
            f"--secret-ref {entry.get('secret_ref')} --endpoint "
            f"'{json.dumps(verifying, sort_keys=True)}'`; it keeps the stored secret, "
            f"send capability, aliases and auth-mode untouched.")
        remediation.append({"step": tls_step, "human_input": False})
        next_items.append(tls_step)

    out({"engine": __version__,
         "store_backend": {"name": backend.name, "ok": bool(store_ok)},
         "secret_backend": secret_backend,
         "secret_backend_confirmed": secret_backend_confirmed,
         "secret_backend_hint": secret_backend_hint,
         "accounts": rows,
         "remediation": remediation,
         "next": next_items})

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
    su.add_argument("--secret-backend", dest="secret_backend",
                    choices=["auto", "keyring", "file"], default=None,
                    help="run OS-aware secret provisioning + pre-flight instead of STORE "
                         "setup: 'auto' selects the OS keyring when a Secret-Service "
                         "provider is reachable (else prints OS-specific guidance), "
                         "'keyring' forces it, 'file' is the explicit 0600-file choice.")
    su.set_defaults(func=cmd_setup)
    av = add_parser("apply-validators"); av.set_defaults(func=cmd_apply_validators)

    # embedded mail client (CR-OA-020 §S5) — reference-only auth + read verbs
    msr = add_parser(
        "mail-search",
        description=(
            "Search the configured mailboxes with a portable compound query, "
            "merged + de-duped across accounts. Supported grammar: qualifiers "
            "(subject:, from:, category:, newer_than:, has:attachment), the OR "
            "operator, parenthesised groups, and quoted-phrase (\"exact phrase\") "
            "matching. Example: category:purchases \"out for delivery\""
        ),
    )
    msr.add_argument(
        "query",
        help=(
            "portable compound query: qualifiers + OR + parenthesised groups + "
            "quoted \"exact phrase\" matching, e.g. category:purchases "
            "\"out for delivery\""
        ),
    )
    msr.add_argument("--accounts", type=lambda s: s.split(",") if s else None,
                     help="comma-separated account names to search (default: all)")
    read_json(msr); read_full(msr); msr.set_defaults(func=cmd_mail_search)
    mac = add_parser("mail-accounts"); read_json(mac); mac.set_defaults(func=cmd_mail_accounts)
    mge = add_parser("mail-get"); mge.add_argument("--account", required=True)
    mge.add_argument("--uid", required=True); read_json(mge); mge.set_defaults(func=cmd_mail_get)
    mex = add_parser("mail-extract"); mex.add_argument("--account", required=True)
    mex.add_argument("--uid", required=True); read_json(mex); mex.set_defaults(func=cmd_mail_extract)
    mau = add_parser("mail-auth")
    mau.add_argument("--provider", required=True,
                     help="mail provider, e.g. fastmail / gmail / yahoo "
                          "(one of the supported providers).")
    mau.add_argument("--address", required=True,
                     help="your mailbox address for this account, e.g. "
                          "you@fastmail.com (a sample format only; supply your own).")
    mau.add_argument("--auth-mode", dest="auth_mode",
                     choices=["password", "xoauth2"], default=None,
                     help="gmail only: 'xoauth2' expects the secret to be a JSON blob "
                          "{client_id, client_secret, refresh_token}; default 'password' "
                          "(on a re-registration, the account's existing auth-mode).")
    mau.add_argument("--secret-ref", dest="secret_ref", default=None,
                     help="credential reference (keyring/file). Omit to be prompted "
                          "(hidden) or to pipe the secret on stdin; it is stored under a "
                          "derived reference and never accepted as a CLI arg.")
    mau.add_argument("--send", action="store_true", dest="send",
                     help="grant this account SEND capability (opt-in; read-only by "
                          "default). The send verbs refuse a non-send-capable account.")
    mau.add_argument("--alias", action="append", dest="alias", default=None,
                     help="an additional From identity for this account (a configured "
                          "Fastmail masked alias, etc.). Repeatable; the From-identity "
                          "guard accepts the account address plus every configured alias.")
    mau.add_argument("--endpoint", dest="endpoint", default=None,
                     help="OPTIONAL provider-endpoint override, a JSON object with any "
                          "of {jmap_url, imap_host, imap_port, smtp_host, smtp_port, "
                          "tls_verify}, pointing this account's adapter at a local "
                          "emulator instead of the real provider. Omit to keep the real "
                          "provider defaults (or whatever override is already "
                          "configured); pass '{}' to CLEAR a configured override — "
                          "including a tls_verify:false opt-out. 'voa doctor' reports "
                          "the override and the TLS-verification state per account.")
    read_json(mau); mau.set_defaults(func=cmd_mail_auth)
    # CR-OA-022 §S3: draft-then-confirm send verbs. `--from` -> dest `from_addr`
    # (``from`` is a Python keyword). `--attach`/`--case` parse now (attachment
    # bodies land in §S6, store linkage in §S5) so the flags are accepted today.
    mdr = add_parser("mail-draft")
    mdr.add_argument("--account", required=True)
    mdr.add_argument("--from", dest="from_addr", required=True)
    mdr.add_argument("--to", required=True)
    mdr.add_argument("--subject", required=True)
    mdr.add_argument("--body", required=True)
    mdr.add_argument("--cc", default=None)
    mdr.add_argument("--attach", default=None)
    mdr.add_argument("--case", default=None)
    mdr.add_argument("--invoice", default=None)
    mdr.add_argument("--warranty", default=None)
    mdr.add_argument("--order", default=None)
    mdr.add_argument("--force", action="store_true", dest="force",
                     help="bypass the verified-recipient guard and draft to an "
                          "un-verified recipient anyway (still never sends).")
    read_json(mdr); mdr.set_defaults(func=cmd_mail_draft)
    msn = add_parser("mail-send")
    msn.add_argument("--account", required=True)
    msn.add_argument("--draft", required=True)
    read_json(msn); msn.set_defaults(func=cmd_mail_send)
    mrp = add_parser("mail-reply")
    mrp.add_argument("--account", required=True)
    mrp.add_argument("--uid", required=True)
    mrp.add_argument("--from", dest="from_addr", required=True)
    mrp.add_argument("--body", required=True)
    mrp.add_argument("--attach", default=None)
    mrp.add_argument("--case", default=None)
    mrp.add_argument("--invoice", default=None)
    mrp.add_argument("--warranty", default=None)
    mrp.add_argument("--order", default=None)
    mrp.add_argument("--force", action="store_true", dest="force",
                     help="bypass the verified-recipient guard and reply to an "
                          "un-verified sender anyway (still never sends).")
    read_json(mrp); mrp.set_defaults(func=cmd_mail_reply)
    dr = add_parser("doctor"); read_json(dr)
    dr.add_argument("--fix", action="store_true", dest="fix",
                    help="instantiate the interactive mail-auth secret-entry for each "
                         "account whose reference does not resolve (hidden prompt / "
                         "stdin only; the secret is never accepted as a CLI arg)")
    dr.set_defaults(func=cmd_doctor)

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
