"""CR-OA-030 §S1 — drop the non-conformant `deliveredTo` JMAP projection (RED).

A compliant JMAP server rejects an `Email/get` projection that requests
`deliveredTo` (not an RFC 8621 `Email` property) with a method-level
`["error", {"type": "invalidArguments", "description": "Invalid property
deliveredTo"}, callId]` INSIDE HTTP 200 — which is exactly what `_parse()`
silently swallows to an empty list today. §S1 replaces `deliveredTo` with the
conformant delivered-to **header projection** (`header:Delivered-To:asText:all`)
and makes `_build_message` read `Message.delivered_to` from that header key
instead. See `docs/changes/CR-OA-030-jmap-read-path-correctness.md` §S1 and
`docs/research/DN-mail-access.md` §"Decision 2 — revision (2026-07-29)".

Every test here targets the current (defective) code and is expected to FAIL:
`_EMAIL_PROPERTIES` still carries `deliveredTo` and lacks the header
projection, and `_build_message` still reads `item.get("deliveredTo")`.

No real network — a small in-file `FakeTransport` records every
`(method, url, headers, body)` call and returns canned `(status, dict)`
tuples, matching the seam `tests/test_cr_oa_020_jmap.py` already uses.
"""
import unittest

from vidushi_oa.mail import jmap
from vidushi_oa.mail.jmap import JmapAdapter

SESSION_URL = "https://api.fastmail.com/jmap/session"
API_URL = "https://api.fastmail.com/jmap/api/"
ACCOUNT_ID = "u1234567"

_CANNED_SESSION = {
    "apiUrl": API_URL,
    "accounts": {
        ACCOUNT_ID: {
            "name": "you@fastmail.com",
            "isPersonal": True,
            "accountCapabilities": {"urn:ietf:params:jmap:mail": {}},
        },
    },
    "primaryAccounts": {"urn:ietf:params:jmap:mail": ACCOUNT_ID},
    "capabilities": {"urn:ietf:params:jmap:mail": {}},
}


def _canned_email_get_response(ids=("Ma1",)):
    """A canned batched `methodResponses` body carrying the conformant header
    projection key (NOT `deliveredTo`) for the one known id."""
    items = {
        "Ma1": {
            "id": "Ma1",
            "threadId": "Ta1",
            "messageId": ["<msg1@example.com>"],
            "subject": "Invoice from Acme",
            "from": [{"name": "Acme Billing", "email": "billing@acme.example"}],
            "to": [{"name": "Alex Doe", "email": "you@fastmail.com"}],
            "receivedAt": "2026-07-20T10:00:00Z",
            "header:Delivered-To:asText:all": "purchases-alias@fastmail.com",
        },
    }
    return {
        "methodResponses": [
            ["Email/query", {"accountId": ACCOUNT_ID, "ids": list(ids)}, "0"],
            [
                "Email/get",
                {
                    "accountId": ACCOUNT_ID,
                    "list": [items[i] for i in ids],
                    "notFound": [],
                },
                "1",
            ],
        ],
    }


class FakeTransport:
    """Records every `(method, url, headers, body)` call and returns canned
    `(status, dict)` tuples shaped like real JMAP HTTP responses — no network."""

    def __init__(self, session_response=None, post_response=None):
        self.session_response = session_response or _CANNED_SESSION
        self.post_response = post_response or _canned_email_get_response()
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        if method == "GET":
            return 200, self.session_response
        return 200, self.post_response

    def calls_of(self, method):
        return [c for c in self.calls if c[0] == method]


class EmailPropertiesProjectionTest(unittest.TestCase):
    """§S1 AC1: `_EMAIL_PROPERTIES` contains no `deliveredTo` entry; every
    remaining entry is an RFC 8621 `Email` property or the delivered-to
    header projection (`header:Delivered-To:asText:all`). Fails today because
    `deliveredTo` is present and the header projection is absent."""

    def test_email_properties_excludes_deliveredto(self):
        self.assertNotIn("deliveredTo", jmap._EMAIL_PROPERTIES)

    def test_email_properties_matches_the_exact_conformant_set(self):
        self.assertEqual(
            set(jmap._EMAIL_PROPERTIES),
            {
                "id",
                "threadId",
                "messageId",
                "subject",
                "from",
                "to",
                "receivedAt",
                "header:Delivered-To:asText:all",
            },
        )


class BuildMessageDeliveredToFromHeaderProjectionTest(unittest.TestCase):
    """§S1 AC2: `_build_message` populates `Message.delivered_to` from the
    header projection key (`header:Delivered-To:asText:all`) when present in
    the `Email/get` item, and `""` when absent — with no raise. Fails today
    because `_build_message` reads `item.get("deliveredTo")` instead."""

    def setUp(self):
        self.adapter = JmapAdapter(
            account="fastmail_main", source_tag="[FM]", token="secret-token",
            session_url=SESSION_URL, transport=FakeTransport(),
        )

    def test_delivered_to_is_populated_from_the_header_projection_key_when_present(self):
        item = {
            "id": "Ma1",
            "threadId": "Ta1",
            "messageId": ["<msg1@example.com>"],
            "subject": "Invoice from Acme",
            "from": [{"name": "Acme Billing", "email": "billing@acme.example"}],
            "to": [{"name": "Alex Doe", "email": "you@fastmail.com"}],
            "receivedAt": "2026-07-20T10:00:00Z",
            "header:Delivered-To:asText:all": "alias@example.com",
        }

        message = self.adapter._build_message(item)

        self.assertEqual(message.delivered_to, "alias@example.com")

    def test_delivered_to_is_empty_string_when_header_projection_key_absent(self):
        # The item carries the OLD, non-conformant `deliveredTo` property (as a
        # non-compliant/legacy server response might) but NOT the new header
        # projection key. `_build_message` must ignore `deliveredTo` entirely
        # post-fix and degrade to "" — proving it reads the header key, not
        # the retired property. Today's code reads `item.get("deliveredTo")`
        # and would wrongly return the legacy value instead of "".
        item = {
            "id": "Ma2",
            "threadId": "Ta2",
            "messageId": ["<msg2@example.com>"],
            "subject": "Your receipt",
            "from": [{"name": "Shop Receipts", "email": "receipts@shop.example"}],
            "to": [{"name": "Alex Doe", "email": "you@fastmail.com"}],
            "receivedAt": "2026-07-21T11:00:00Z",
            "deliveredTo": "stale-legacy-value@fastmail.com",
        }

        message = self.adapter._build_message(item)

        self.assertEqual(message.delivered_to, "")


class SearchRequestProjectionConformanceTest(unittest.TestCase):
    """§S1 AC3 (request-shape guard): the `properties` list our adapter SENDS
    in the `Email/get` half of `search()`'s batched request is conformant — no
    `deliveredTo`, but does carry the header projection. The CR-OA-020 fakes
    only ever proved we can PARSE a `deliveredTo` field in a canned response,
    never that a real server accepts it in an outgoing request. Fails today
    because `_EMAIL_PROPERTIES` (sent verbatim as `properties`) still carries
    `deliveredTo` and lacks the header projection."""

    def test_search_sends_a_conformant_email_get_properties_projection(self):
        transport = FakeTransport()
        adapter = JmapAdapter(
            account="fastmail_main", source_tag="[FM]", token="secret-token",
            session_url=SESSION_URL, transport=transport,
        )

        adapter.search("invoice")

        _, _, _, body = transport.calls_of("POST")[0]
        get_call = body["methodCalls"][1]
        self.assertEqual(get_call[0], "Email/get")
        properties = get_call[1]["properties"]
        self.assertNotIn("deliveredTo", properties)
        self.assertIn("header:Delivered-To:asText:all", properties)


if __name__ == "__main__":
    unittest.main()
