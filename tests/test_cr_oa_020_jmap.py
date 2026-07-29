"""CR-OA-020 §S3 — Fastmail JMAP adapter (thin-HTTP) + IMAP fallback (RED).

`vidushi_oa/mail/jmap.py` does not exist yet, so every import below fails with
`ModuleNotFoundError` until GREEN lands:

  - `JmapAdapter(MailAdapter)` — `__init__(account, source_tag, token,
    session_url=..., transport=None)`. `transport` is an injected callable
    `(method, url, headers, body_dict_or_None) -> (status_int, response_dict)`
    (default would be a stdlib-`urllib` transport — never exercised here).
    `_session()` issues ONE `transport("GET", session_url,
    {"Authorization": "Bearer <token>"}, None)` call, caching the returned
    `apiUrl` and the primary mail `accountId` (`primaryAccounts
    ["urn:ietf:params:jmap:mail"]`) for every subsequent call — a second
    `search()` must NOT re-fetch the session.
  - `search(query, folder=None, limit=None)` issues exactly ONE
    `transport("POST", apiUrl, ...)` per call. The POST body carries `using`
    including the mail capability, and `methodCalls` =
    `[["Email/query", {"accountId": ..., "filter": {"text": query}}, "0"],
      ["Email/get", {"accountId": ..., "#ids": {"resultOf": "0",
      "name": "Email/query", "path": "/ids"}, "properties": [...]}, "1"]]` —
    a back-referenced batch, with a bounded `properties` projection (no full
    body/attachments). The `Email/get` result list is parsed into `Message`s:
    `id` from `messageId[0]` (the RFC `Message-ID`), `thread_id` from
    `threadId`, subject/sender/to/date from the matching JMAP properties, and
    the delivered-to alias (masked-alias trick) surfaced as `delivered_to`
    (preferring the JMAP `deliveredTo` property when present).
  - `capabilities()` = `{"server_threads", "server_side_search", "projection"}`
    (JMAP has first-class threads, a server-side filter, and property
    projection; it is NOT `raw_query` — that's Gmail's X-GM-RAW extension).
  - `fastmail_adapter(account, source_tag, config, transport=None,
    conn_factory=None)` — a fallback selector: a `config` carrying a JMAP
    token (`config["jmap_token"]`) returns a `JmapAdapter`; a Basic-plan
    `config` with only an app password falls back to an `ImapAdapter` against
    `imap.fastmail.com` with source_tag `[FM]`.

No real network — a small in-file `FakeTransport` records every
`(method, url, headers, body)` call and returns canned `(status, dict)`
tuples: a canned JMAP session dict for the GET, and a canned batched
`methodResponses` dict for the POST.
"""
import unittest

from vidushi_oa.mail.imap import ImapAdapter
from vidushi_oa.mail.jmap import JmapAdapter, fastmail_adapter

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


def _canned_email_get_response(ids=("Ma1", "Ma2")):
    """A canned batched `methodResponses` body for the two known ids."""
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
        "Ma2": {
            "id": "Ma2",
            "threadId": "Ta2",
            "messageId": ["<msg2@example.com>"],
            "subject": "Your receipt",
            "from": [{"name": "Shop Receipts", "email": "receipts@shop.example"}],
            "to": [{"name": "Alex Doe", "email": "you@fastmail.com"}],
            "receivedAt": "2026-07-21T11:00:00Z",
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


class JmapSessionCachedOnceTest(unittest.TestCase):
    """§S3 AC: the session GET fires exactly once no matter how many `search()`
    calls follow — the second reuses the cached `apiUrl`/`accountId`."""

    def test_two_searches_fetch_the_session_exactly_once(self):
        transport = FakeTransport()
        adapter = JmapAdapter(
            account="fastmail_main", source_tag="[FM]", token="secret-token",
            session_url=SESSION_URL, transport=transport,
        )

        adapter.search("invoice")
        adapter.search("invoice")

        get_calls = transport.calls_of("GET")
        self.assertEqual(len(get_calls), 1)

    def test_session_get_uses_bearer_auth_header_and_session_url(self):
        transport = FakeTransport()
        adapter = JmapAdapter(
            account="fastmail_main", source_tag="[FM]", token="secret-token",
            session_url=SESSION_URL, transport=transport,
        )

        adapter.search("invoice")

        method, url, headers, body = transport.calls_of("GET")[0]
        self.assertEqual(url, SESSION_URL)
        self.assertEqual(headers["Authorization"], "Bearer secret-token")
        self.assertIsNone(body)

    def test_both_searches_post_to_the_cached_api_url(self):
        transport = FakeTransport()
        adapter = JmapAdapter(
            account="fastmail_main", source_tag="[FM]", token="secret-token",
            session_url=SESSION_URL, transport=transport,
        )

        adapter.search("invoice")
        adapter.search("receipt")

        post_calls = transport.calls_of("POST")
        self.assertEqual(len(post_calls), 2)
        self.assertEqual(post_calls[0][1], API_URL)
        self.assertEqual(post_calls[1][1], API_URL)


class JmapOneBatchedPostTest(unittest.TestCase):
    """§S3 AC: a single `search()` issues exactly ONE POST whose body batches
    an `Email/query` (call id "0") and a back-referenced `Email/get` (via the
    `#ids` result reference), with a bounded (non-body) properties list."""

    def setUp(self):
        self.transport = FakeTransport()
        self.adapter = JmapAdapter(
            account="fastmail_main", source_tag="[FM]", token="secret-token",
            session_url=SESSION_URL, transport=self.transport,
        )

    def test_search_issues_exactly_one_post(self):
        self.adapter.search("invoice")

        self.assertEqual(len(self.transport.calls_of("POST")), 1)

    def test_post_body_includes_mail_capability_in_using(self):
        self.adapter.search("invoice")

        _, _, _, body = self.transport.calls_of("POST")[0]
        self.assertIn("urn:ietf:params:jmap:mail", body["using"])

    def test_post_body_has_email_query_call_with_the_query_text(self):
        self.adapter.search("invoice")

        _, _, _, body = self.transport.calls_of("POST")[0]
        method_calls = body["methodCalls"]
        query_call = method_calls[0]
        self.assertEqual(query_call[0], "Email/query")
        self.assertEqual(query_call[1]["accountId"], ACCOUNT_ID)
        self.assertEqual(query_call[1]["filter"], {"text": "invoice"})
        self.assertEqual(query_call[2], "0")

    def test_post_body_has_email_get_back_referencing_the_query_by_result_reference(self):
        self.adapter.search("invoice")

        _, _, _, body = self.transport.calls_of("POST")[0]
        method_calls = body["methodCalls"]
        get_call = method_calls[1]
        self.assertEqual(get_call[0], "Email/get")
        self.assertEqual(get_call[1]["accountId"], ACCOUNT_ID)
        self.assertEqual(
            get_call[1]["#ids"],
            {"resultOf": "0", "name": "Email/query", "path": "/ids"},
        )
        self.assertEqual(get_call[2], "1")

    def test_email_get_properties_are_bounded_and_exclude_the_full_body(self):
        self.adapter.search("invoice")

        _, _, _, body = self.transport.calls_of("POST")[0]
        properties = body["methodCalls"][1][1]["properties"]
        for needed in ("id", "threadId", "messageId", "subject", "from", "to"):
            self.assertIn(needed, properties)
        for heavy in ("textBody", "htmlBody", "bodyStructure", "attachments", "bodyValues"):
            self.assertNotIn(heavy, properties)


class JmapParseEmailGetIntoMessagesTest(unittest.TestCase):
    """§S3 AC: the canned `Email/get` list parses into `Message`s with the
    right id (from `messageId`), thread_id, subject, and source_tag."""

    def setUp(self):
        self.transport = FakeTransport()
        self.adapter = JmapAdapter(
            account="fastmail_main", source_tag="[FM]", token="secret-token",
            session_url=SESSION_URL, transport=self.transport,
        )

    def test_search_returns_two_messages_parsed_from_the_canned_list(self):
        results = self.adapter.search("invoice")

        self.assertEqual(len(results), 2)

    def test_message_id_comes_from_the_jmap_message_id_header(self):
        results = self.adapter.search("invoice")

        by_thread = {m.thread_id: m for m in results}
        self.assertEqual(by_thread["Ta1"].id, "<msg1@example.com>")
        self.assertEqual(by_thread["Ta2"].id, "<msg2@example.com>")

    def test_thread_id_comes_from_the_jmap_thread_id(self):
        results = self.adapter.search("invoice")

        ids_to_threads = {m.id: m.thread_id for m in results}
        self.assertEqual(ids_to_threads["<msg1@example.com>"], "Ta1")
        self.assertEqual(ids_to_threads["<msg2@example.com>"], "Ta2")

    def test_subject_and_source_tag_are_populated(self):
        results = self.adapter.search("invoice")

        by_id = {m.id: m for m in results}
        first = by_id["<msg1@example.com>"]
        self.assertEqual(first.subject, "Invoice from Acme")
        self.assertEqual(first.source_tag, "[FM]")
        self.assertIn("billing@acme.example", first.sender)


class JmapDeliveredToAliasTest(unittest.TestCase):
    """§S3 AC: the delivered-to alias (Fastmail's masked-alias correlation key)
    is surfaced on the parsed `Message`, preferring the JMAP `deliveredTo`
    property over the plain `to` recipient when both are present."""

    def test_delivered_to_alias_is_surfaced_and_differs_from_the_to_recipient(self):
        transport = FakeTransport()
        adapter = JmapAdapter(
            account="fastmail_main", source_tag="[FM]", token="secret-token",
            session_url=SESSION_URL, transport=transport,
        )

        results = adapter.search("invoice")

        by_id = {m.id: m for m in results}
        first = by_id["<msg1@example.com>"]
        self.assertEqual(first.delivered_to, "purchases-alias@fastmail.com")
        self.assertNotIn("purchases-alias@fastmail.com", first.to)


class JmapCapabilitiesTest(unittest.TestCase):
    """§S3 AC: JMAP capabilities are exactly {"server_threads",
    "server_side_search", "projection"} — no `raw_query` (that's Gmail's)."""

    def test_capabilities_is_exactly_the_documented_set(self):
        transport = FakeTransport()
        adapter = JmapAdapter(
            account="fastmail_main", source_tag="[FM]", token="secret-token",
            session_url=SESSION_URL, transport=transport,
        )

        self.assertEqual(
            adapter.capabilities(),
            {"server_threads", "server_side_search", "projection", "send"},
        )
        self.assertNotIn("raw_query", adapter.capabilities())


class FastmailAdapterFallbackSelectorTest(unittest.TestCase):
    """§S3 AC: `fastmail_adapter()` returns a `JmapAdapter` when the config
    carries a JMAP token, and falls back to an IMAP adapter for a Basic-plan
    config that only has an app password."""

    def test_config_with_jmap_token_returns_a_jmap_adapter(self):
        transport = FakeTransport()
        adapter = fastmail_adapter(
            account="fastmail_main", source_tag="[FM]",
            config={"jmap_token": "secret-token"}, transport=transport,
        )

        self.assertIsInstance(adapter, JmapAdapter)

    def test_config_without_jmap_token_falls_back_to_imap_adapter(self):
        adapter = fastmail_adapter(
            account="fastmail_main", source_tag="[FM]",
            config={"app_password": "app-pw-123"},
        )

        self.assertIsInstance(adapter, ImapAdapter)
        self.assertNotIsInstance(adapter, JmapAdapter)

    def test_imap_fallback_targets_fastmail_host_with_fm_source_tag(self):
        adapter = fastmail_adapter(
            account="fastmail_main", source_tag="[FM]",
            config={"app_password": "app-pw-123"},
        )

        self.assertEqual(adapter.host, "imap.fastmail.com")
        self.assertEqual(adapter.source_tag, "[FM]")
        self.assertEqual(adapter.password, "app-pw-123")


if __name__ == "__main__":
    unittest.main()
