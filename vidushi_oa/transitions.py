#!/usr/bin/env python3
"""CR-OA-005 — declarative transition-map engine for the tracking-state framework.

`TRANSITIONS[type]` is a list of transition dicts, each shaped:

    {"from": <STATUS>, "event": <slug>, "to": <STATUS>, "owner": "agent"|"user",
     "effects": [<effect>, ...]}

An effect is one of:

    {"op": "open-action",    "action": <slug>, "owner": "user"|"agent", "detail"?: str}
    {"op": "resolve-action", "action": <slug>}
    {"op": "require-doc",    "type": <slug>}

`store.py event <type> <id> <event>` looks up the doc's current `status` in the
table (via `find_transition`), applies the matching transition (setting `status`
and firing its effects) and rejects any event with no matching `(from, event)`
pair. subscriptions/insurance are declared here for CR-OA-007's due-sweep; only
invoices/warranties are exercised by CR-OA-005's tests.
"""

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
}


def find_transition(t, status, event):
    """Return the transition dict matching `(status, event)` for store type `t`, or None."""
    for tr in TRANSITIONS.get(t, []):
        if tr["from"] == status and tr["event"] == event:
            return tr
    return None
