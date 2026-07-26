#!/usr/bin/env python3
"""CR-OA-005 — declarative transition-map engine for the tracking-state framework.

`TRANSITIONS[type]` is a list of transition dicts, each shaped:

    {"from": <STATUS>, "event": <slug>, "to": <STATUS>, "owner": "agent"|"user",
     "effects": [<effect>, ...]}

An effect is one of:

    {"op": "open-action",    "action": <slug>, "owner": "user"|"agent", "detail"?: str,
                             "by_disposition"?: {<UPPER-CASE disposition>: <slug>}}
    {"op": "resolve-action", "action": <slug>}
    {"op": "require-doc",    "type": <slug>}
    {"op": "set-stage",      "stage": <str>}   # set the human-readable `stage` field
                                               # (CR-OA-015 orders; distinct from `status`)

`by_disposition` (optional, open-action only) overrides the opened action's slug
when the doc's `disposition` (upper-cased) matches a key — e.g. a subscriptions
`renewal-window` opens `renewal-confirm` for a KEEP sub, else `cancel-before-charge`.

`store.py event <type> <id> <event>` looks up the doc's current `status` in the
table (via `find_transition`), applies the matching transition (setting `status`
and firing its effects) and rejects any event with no matching `(from, event)`
pair. subscriptions/insurance are declared here for CR-OA-007's due-sweep; only
invoices/warranties are exercised by CR-OA-005's tests.
"""

# orders (CR-OA-015): fulfilment lifecycle, grounded in DN-purchases-persistence. The
# coarse `status` stays the 4-value tracker; the human-readable `stage` carries the fine
# detail. Stage advances hold IN_PROGRESS; customs sub-states open an OPEN action and hold
# IN_PROGRESS (so `attention` surfaces the parcel); delivered + the side-states land
# COMPLETED with the flavour recorded in `stage`. Every event fires from NEW or a live
# IN_PROGRESS order (an order may be NEW when customs first hits, e.g. a bare-AWB row).
_ORD_STAGE_ADVANCE = {"shipped": "Shipped", "out-for-delivery": "Out for delivery"}
_ORD_CUSTOMS = {"held-at-customs": "customs-clearance", "duty-demanded": "duty-payment",
                "kyc-requested": "kyc", "clarification-requested": "clarification"}
_ORD_TERMINAL = {"delivered": "Delivered", "cancelled": "Cancelled", "returned": "Returned",
                 "refunded": "Refunded", "delivery-failed": "Delivery-failed"}


def _build_orders_table():
    rows = []
    for frm in ("NEW", "IN_PROGRESS"):
        for event, stage in _ORD_STAGE_ADVANCE.items():
            rows.append({"from": frm, "event": event, "to": "IN_PROGRESS", "owner": "agent",
                         "effects": [{"op": "set-stage", "stage": stage}]})
        for event, action in _ORD_CUSTOMS.items():
            rows.append({"from": frm, "event": event, "to": "IN_PROGRESS", "owner": "agent",
                         "effects": [{"op": "open-action", "action": action, "owner": "user"}]})
        for event, stage in _ORD_TERMINAL.items():
            rows.append({"from": frm, "event": event, "to": "COMPLETED", "owner": "agent",
                         "effects": [{"op": "set-stage", "stage": stage}]})
    return rows


TRANSITIONS = {
    "invoices": [
        {"from": "NEW", "event": "paid", "to": "IN_PROGRESS", "owner": "agent", "effects": []},
        {"from": "NEW", "event": "shipped", "to": "IN_PROGRESS", "owner": "agent", "effects": []},
        {"from": "IN_PROGRESS", "event": "delivered", "to": "COMPLETED", "owner": "agent", "effects": []},
    ],
    "warranties": [
        {"from": "IN_PROGRESS", "event": "expire", "to": "EXPIRED", "owner": "agent",
         "effects": [{"op": "open-action", "action": "renew-or-extend", "owner": "user",
                      "detail": "warranty expired - renew or extend/AMC?"}]},
        {"from": "EXPIRED", "event": "renew", "to": "IN_PROGRESS", "owner": "user", "effects": []},
    ],
    "subscriptions": [
        {"from": "IN_PROGRESS", "event": "renewal-window", "to": "DUE", "owner": "agent",
         "effects": [{"op": "open-action", "action": "cancel-before-charge", "owner": "user",
                      "by_disposition": {"KEEP": "renewal-confirm"}}]},
        {"from": "DUE", "event": "renewed", "to": "IN_PROGRESS", "owner": "agent", "effects": []},
        {"from": "DUE", "event": "cancelled", "to": "COMPLETED", "owner": "user", "effects": []},
    ],
    "insurance": [
        {"from": "IN_PROGRESS", "event": "renewal-window", "to": "DUE", "owner": "agent",
         "effects": [{"op": "open-action", "action": "renew-policy", "owner": "user"}]},
        {"from": "DUE", "event": "renewed", "to": "IN_PROGRESS", "owner": "agent", "effects": []},
        {"from": "DUE", "event": "lapsed", "to": "COMPLETED", "owner": "agent", "effects": []},
    ],
    "orders": _build_orders_table(),
}


def find_transition(t, status, event):
    """Return the transition dict matching `(status, event)` for store type `t`, or None."""
    for tr in TRANSITIONS.get(t, []):
        if tr["from"] == status and tr["event"] == event:
            return tr
    return None
