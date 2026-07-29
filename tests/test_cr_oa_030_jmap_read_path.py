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


class BuildMessageDeliveredToArrayShapeTest(unittest.TestCase):
    """§S1 AC2 (real-server shape): RFC 8621 §4.1.4 defines the `:all` suffix as
    returning a JSON ARRAY of strings — every header field with that name — not
    a scalar. Every fake in `tests/` hands `_build_message` a plain string, so
    the unit tier cannot see that a real server's array value lands on
    `Message.delivered_to` verbatim as a `list`. `delivered_to` must ALWAYS be a
    `str`: a list/tuple collapses to its first non-empty (stripped) entry, a
    plain string passes through, and absent/None/empty degrades to `""`."""

    def setUp(self):
        self.adapter = JmapAdapter(
            account="fastmail_main", source_tag="[FM]", token="secret-token",
            session_url=SESSION_URL, transport=FakeTransport(),
        )

    def _item(self, delivered_to_value):
        item = {
            "id": "Ma1",
            "threadId": "Ta1",
            "messageId": ["<msg1@example.com>"],
            "subject": "Invoice from Acme",
            "from": [{"name": "Acme Billing", "email": "billing@acme.example"}],
            "to": [{"name": "Alex Doe", "email": "you@fastmail.com"}],
            "receivedAt": "2026-07-20T10:00:00Z",
        }
        if delivered_to_value is not None:
            item["header:Delivered-To:asText:all"] = delivered_to_value
        return item

    def test_single_element_array_yields_that_address_as_a_string(self):
        message = self.adapter._build_message(self._item(["alias@example.com"]))

        self.assertIsInstance(message.delivered_to, str)
        self.assertEqual(message.delivered_to, "alias@example.com")

    def test_multi_value_array_yields_the_first_address_as_a_string(self):
        message = self.adapter._build_message(
            self._item(["first@example.com", "second@example.com"]))

        self.assertIsInstance(message.delivered_to, str)
        self.assertEqual(message.delivered_to, "first@example.com")

    def test_array_entries_are_stripped_and_empty_leading_entries_skipped(self):
        # `asText` values carry the header's leading fold whitespace; an empty
        # first field must not mask a real address behind it.
        message = self.adapter._build_message(
            self._item(["", "  alias@example.com  "]))

        self.assertEqual(message.delivered_to, "alias@example.com")

    def test_empty_array_yields_the_empty_string(self):
        message = self.adapter._build_message(self._item([]))

        self.assertIsInstance(message.delivered_to, str)
        self.assertEqual(message.delivered_to, "")

    def test_all_blank_array_yields_the_empty_string(self):
        message = self.adapter._build_message(self._item(["", "   "]))

        self.assertIsInstance(message.delivered_to, str)
        self.assertEqual(message.delivered_to, "")

    def test_none_value_yields_the_empty_string(self):
        # A server that saw no Delivered-To field returns JSON `null`.
        message = self.adapter._build_message(self._item(None))
        self.assertEqual(message.delivered_to, "")

        explicit_null = self._item(["placeholder"])
        explicit_null["header:Delivered-To:asText:all"] = None
        self.assertEqual(self.adapter._build_message(explicit_null).delivered_to, "")

    def test_plain_string_still_passes_through_stripped(self):
        message = self.adapter._build_message(self._item(" alias@example.com "))

        self.assertIsInstance(message.delivered_to, str)
        self.assertEqual(message.delivered_to, "alias@example.com")


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
