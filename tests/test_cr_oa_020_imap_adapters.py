"""CR-OA-020 §S2 — IMAP adapters: Gmail (`X-GM-RAW`) + Yahoo (RFC 3501) (RED).

`vidushi_oa/mail/imap.py` does not exist yet, so every import below fails with
`ModuleNotFoundError` until GREEN lands:

  - `ImapAdapter(MailAdapter)` — `_conn()` lazily creates the connection via an
    injected `conn_factory(host, port)`, logs in + selects, and CACHES/REUSES it
    (created at most once) across `search`/`fetch_message`/`list_folders`. Header
    parsing pulls `Message-ID`/Subject/From/To/Date out of a
    `FETCH (BODY.PEEK[HEADER.FIELDS (...)])` response into a `Message`.
  - `GmailImapAdapter(ImapAdapter)` — `capabilities()` =
    `{"raw_query", "server_side_categories", "server_threads"}`; `search()` issues
    `conn.uid("SEARCH", "X-GM-RAW", <quoted gmail query>)`, then a UID FETCH that
    also requests `X-GM-THRID` and sets `Message.thread_id` from it.
  - `YahooImapAdapter(ImapAdapter)` — `capabilities()` has none of Gmail's three
    flags; `search()` issues a plain RFC 3501 `uid("SEARCH", ...)` (no `X-GM-*`
    token), reconstructs `thread_id` client-side from `References`/`In-Reply-To`,
    and must not select `Bulk Mail` by default.

No real IMAP/network — a small in-file `FakeIMAP` records every `.uid(cmd, *args)`
/ `.login` / `.select` / `.list` call and returns canned `(typ, data)` tuples shaped
like real `imaplib` (`SEARCH` -> `[b"1 2 3"]`; `FETCH` -> a list of
`(descriptor_bytes, header_bytes)` tuples interleaved with closing `b")"` markers,
matching what `imaplib.IMAP4.uid()` actually returns). It is injected into the
adapters via `conn_factory`.
"""
import unittest

from vidushi_oa.mail.imap import GmailImapAdapter, ImapAdapter, YahooImapAdapter


def _headers(subject, sender, to, date, message_id, references=None, in_reply_to=None):
    lines = [
        f"Subject: {subject}",
        f"From: {sender}",
        f"To: {to}",
        f"Date: {date}",
        f"Message-ID: {message_id}",
    ]
    if references:
        lines.append(f"References: {references}")
    if in_reply_to:
        lines.append(f"In-Reply-To: {in_reply_to}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


def _fetch_tuple(msg_num, uid, header_bytes, thrid=None):
    descriptor = f"{msg_num} (UID {uid}"
    if thrid is not None:
        descriptor += f" X-GM-THRID {thrid}"
    descriptor += (
        " BODY[HEADER.FIELDS (SUBJECT FROM TO DATE MESSAGE-ID REFERENCES "
        f"IN-REPLY-TO)] {{{len(header_bytes)}}}"
    )
    return (descriptor.encode(), header_bytes)


class FakeIMAP:
    """Records every call so tests can assert on the exact IMAP wire shape used,
    and returns canned (typ, data) tuples shaped like real `imaplib`."""

    def __init__(self, search_response=None, fetch_response=None, list_response=None):
        self.search_response = search_response if search_response is not None else ("OK", [b""])
        self.fetch_response = fetch_response if fetch_response is not None else ("OK", [])
        self.list_response = list_response if list_response is not None else (
            "OK",
            [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren) "/" "Bulk Mail"'],
        )
        self.login_calls = []
        self.select_calls = []
        self.uid_calls = []
        self.list_calls = 0

    def login(self, user, password):
        self.login_calls.append((user, password))
        return ("OK", [b"Logged in"])

    def select(self, mailbox="INBOX", readonly=False):
        self.select_calls.append(mailbox)
        return ("OK", [b"1"])

    def list(self, *args):
        self.list_calls += 1
        return self.list_response

    def uid(self, command, *args):
        self.uid_calls.append((command, args))
        if command.upper() == "SEARCH":
            return self.search_response
        if command.upper() == "FETCH":
            return self.fetch_response
        return ("OK", [None])


def _make_conn_factory(fake):
    """Returns (factory, calls) — `calls` records every (host, port) the adapter
    asked the factory to build a connection for, so tests can assert it happens
    at most once even across multiple adapter calls."""
    calls = []

    def factory(host, port):
        calls.append((host, port))
        return fake

    return factory, calls


class GmailXGmRawSearchTest(unittest.TestCase):
    """§S2 AC: against a fake IMAP server, the Gmail adapter issues an `X-GM-RAW`
    search for a Gmail-syntax query and parses the matched UIDs' headers into the
    common `Message` shape, with `thread_id` populated from `X-GM-THRID`."""

    def setUp(self):
        header_1 = _headers(
            "Invoice from Acme", "billing@acme.example", "me@gmail.com",
            "Mon, 20 Jul 2026 10:00:00 +0000", "<msg1@gmail.com>",
        )
        header_2 = _headers(
            "Your receipt", "receipts@shop.example", "me@gmail.com",
            "Tue, 21 Jul 2026 11:00:00 +0000", "<msg2@gmail.com>",
        )
        fetch_response = (
            "OK",
            [
                _fetch_tuple(1, 1, header_1, thrid=100000000001),
                b")",
                _fetch_tuple(2, 2, header_2, thrid=100000000002),
                b")",
            ],
        )
        self.fake = FakeIMAP(
            search_response=("OK", [b"1 2"]),
            fetch_response=fetch_response,
        )
        self.factory, self.factory_calls = _make_conn_factory(self.fake)
        self.adapter = GmailImapAdapter(
            account="gmail_main",
            source_tag="[GM]",
            host="imap.gmail.com",
            user="me@gmail.com",
            password="app-pw",
            conn_factory=self.factory,
        )

    def test_search_issues_x_gm_raw_uid_search_with_the_gmail_query(self):
        self.adapter.search("category:updates newer_than:30d")

        search_calls = [c for c in self.fake.uid_calls if c[0].upper() == "SEARCH"]
        self.assertEqual(len(search_calls), 1)
        _, args = search_calls[0]
        self.assertEqual(args[0], "X-GM-RAW")
        joined = " ".join(str(a) for a in args)
        self.assertIn("category:updates newer_than:30d", joined)

    def test_fetch_uses_body_peek_header_fields_and_requests_gm_thrid(self):
        self.adapter.search("category:updates newer_than:30d")

        fetch_calls = [c for c in self.fake.uid_calls if c[0].upper() == "FETCH"]
        self.assertEqual(len(fetch_calls), 1)
        _, args = fetch_calls[0]
        joined = " ".join(str(a) for a in args).upper()
        self.assertIn("BODY.PEEK", joined)
        self.assertIn("HEADER.FIELDS", joined)
        self.assertIn("MESSAGE-ID", joined)
        self.assertIn("X-GM-THRID", joined)

    def test_search_parses_matched_uids_into_messages_with_id_subject_sender_and_source_tag(self):
        results = self.adapter.search("category:updates newer_than:30d")

        self.assertEqual(len(results), 2)
        by_id = {m.id: m for m in results}
        self.assertIn("<msg1@gmail.com>", by_id)
        self.assertIn("<msg2@gmail.com>", by_id)
        first = by_id["<msg1@gmail.com>"]
        self.assertEqual(first.subject, "Invoice from Acme")
        self.assertEqual(first.sender, "billing@acme.example")
        self.assertEqual(first.source_tag, "[GM]")

    def test_search_sets_thread_id_from_x_gm_thrid(self):
        results = self.adapter.search("category:updates newer_than:30d")

        by_id = {m.id: m for m in results}
        self.assertEqual(str(by_id["<msg1@gmail.com>"].thread_id), "100000000001")
        self.assertEqual(str(by_id["<msg2@gmail.com>"].thread_id), "100000000002")
        self.assertNotEqual(
            by_id["<msg1@gmail.com>"].thread_id, by_id["<msg2@gmail.com>"].thread_id
        )


class GmailCapabilitiesTest(unittest.TestCase):
    """§S2 AC: Gmail capabilities = {"raw_query", "server_side_categories",
    "server_threads"}."""

    def test_gmail_capabilities_exact_set(self):
        fake = FakeIMAP()
        factory, _ = _make_conn_factory(fake)
        adapter = GmailImapAdapter(
            account="gmail_main", source_tag="[GM]", host="imap.gmail.com",
            user="me@gmail.com", password="app-pw", conn_factory=factory,
        )

        self.assertEqual(
            adapter.capabilities(),
            {"raw_query", "server_side_categories", "server_threads", "send"},
        )


class YahooRfc3501SearchTest(unittest.TestCase):
    """§S2 AC: the Yahoo adapter issues a plain RFC 3501 `SEARCH` (no `X-GM-*`);
    capabilities has none of Gmail's flags."""

    def setUp(self):
        header_1 = _headers(
            "Statement ready", "billing@yahoo-vendor.example", "me@yahoo.com",
            "Mon, 20 Jul 2026 09:00:00 +0000", "<y1@yahoo.example>",
        )
        fetch_response = ("OK", [_fetch_tuple(1, 1, header_1), b")"])
        self.fake = FakeIMAP(
            search_response=("OK", [b"1"]),
            fetch_response=fetch_response,
        )
        self.factory, self.factory_calls = _make_conn_factory(self.fake)
        self.adapter = YahooImapAdapter(
            account="yahoo_main",
            source_tag="[YH]",
            host="imap.mail.yahoo.com",
            user="me@yahoo.com",
            password="app-pw",
            conn_factory=self.factory,
        )

    def test_search_issues_plain_rfc3501_search_with_no_gm_tokens(self):
        self.adapter.search("from:vendor")

        search_calls = [c for c in self.fake.uid_calls if c[0].upper() == "SEARCH"]
        self.assertEqual(len(search_calls), 1)
        _, args = search_calls[0]
        joined = " ".join(str(a) for a in args).upper()
        self.assertNotIn("X-GM-RAW", joined)
        self.assertNotIn("X-GM", joined)

    def test_yahoo_capabilities_has_none_of_gmails_flags(self):
        gmail_only_flags = {"raw_query", "server_side_categories", "server_threads"}

        self.assertEqual(self.adapter.capabilities() & gmail_only_flags, set())


class YahooClientSideThreadingTest(unittest.TestCase):
    """§S2 AC: the Yahoo adapter reconstructs a thread from `References` client
    side — a message whose `References` contains another's `Message-ID` gets the
    SAME `thread_id`; an unrelated message gets a different one."""

    def test_related_messages_share_thread_id_unrelated_message_does_not(self):
        header_a = _headers(
            "Order confirmation", "sales@shop.example", "me@yahoo.com",
            "Mon, 20 Jul 2026 09:00:00 +0000", "<a@yahoo.example>",
        )
        header_b = _headers(
            "Re: Order confirmation", "sales@shop.example", "me@yahoo.com",
            "Mon, 20 Jul 2026 10:00:00 +0000", "<b@yahoo.example>",
            references="<a@yahoo.example>", in_reply_to="<a@yahoo.example>",
        )
        header_c = _headers(
            "Completely unrelated newsletter", "news@other.example", "me@yahoo.com",
            "Tue, 21 Jul 2026 08:00:00 +0000", "<c@yahoo.example>",
        )
        fetch_response = (
            "OK",
            [
                _fetch_tuple(1, 1, header_a), b")",
                _fetch_tuple(2, 2, header_b), b")",
                _fetch_tuple(3, 3, header_c), b")",
            ],
        )
        fake = FakeIMAP(search_response=("OK", [b"1 2 3"]), fetch_response=fetch_response)
        factory, _ = _make_conn_factory(fake)
        adapter = YahooImapAdapter(
            account="yahoo_main", source_tag="[YH]", host="imap.mail.yahoo.com",
            user="me@yahoo.com", password="app-pw", conn_factory=factory,
        )

        results = adapter.search("order")

        by_id = {m.id: m for m in results}
        thread_a = by_id["<a@yahoo.example>"].thread_id
        thread_b = by_id["<b@yahoo.example>"].thread_id
        thread_c = by_id["<c@yahoo.example>"].thread_id
        self.assertTrue(thread_a)
        self.assertEqual(thread_a, thread_b)
        self.assertNotEqual(thread_a, thread_c)


class ImapAdapterReusedConnectionTest(unittest.TestCase):
    """§S2 AC: a single reused connection — two consecutive `search()` calls
    create the underlying connection via `conn_factory` at most once, and the
    fake IMAP's `.login` is likewise called exactly once."""

    def test_two_consecutive_searches_create_the_connection_exactly_once(self):
        header_1 = _headers(
            "Ping", "a@example.com", "me@gmail.com",
            "Mon, 20 Jul 2026 09:00:00 +0000", "<one@gmail.com>",
        )
        fetch_response = ("OK", [_fetch_tuple(1, 1, header_1, thrid=1), b")"])
        fake = FakeIMAP(search_response=("OK", [b"1"]), fetch_response=fetch_response)
        factory, factory_calls = _make_conn_factory(fake)
        adapter = GmailImapAdapter(
            account="gmail_main", source_tag="[GM]", host="imap.gmail.com",
            user="me@gmail.com", password="app-pw", conn_factory=factory,
        )

        adapter.search("category:updates")
        adapter.search("category:updates")

        self.assertEqual(len(factory_calls), 1)
        self.assertEqual(len(fake.login_calls), 1)
        self.assertEqual(fake.login_calls[0], ("me@gmail.com", "app-pw"))

    def test_search_then_fetch_message_also_reuses_the_single_connection(self):
        header_1 = _headers(
            "Ping", "a@example.com", "me@gmail.com",
            "Mon, 20 Jul 2026 09:00:00 +0000", "<one@gmail.com>",
        )
        fetch_response = ("OK", [_fetch_tuple(1, 1, header_1, thrid=1), b")"])
        fake = FakeIMAP(search_response=("OK", [b"1"]), fetch_response=fetch_response)
        factory, factory_calls = _make_conn_factory(fake)
        adapter = GmailImapAdapter(
            account="gmail_main", source_tag="[GM]", host="imap.gmail.com",
            user="me@gmail.com", password="app-pw", conn_factory=factory,
        )

        adapter.search("category:updates")
        adapter.fetch_message("1")

        self.assertEqual(len(factory_calls), 1)
        self.assertEqual(len(fake.login_calls), 1)


class YahooSkipsBulkMailTest(unittest.TestCase):
    """§S2 AC: the Yahoo adapter must not select/scan the `Bulk Mail` folder by
    default."""

    def test_search_does_not_select_bulk_mail_by_default(self):
        header_1 = _headers(
            "Statement ready", "billing@yahoo-vendor.example", "me@yahoo.com",
            "Mon, 20 Jul 2026 09:00:00 +0000", "<y1@yahoo.example>",
        )
        fetch_response = ("OK", [_fetch_tuple(1, 1, header_1), b")"])
        fake = FakeIMAP(search_response=("OK", [b"1"]), fetch_response=fetch_response)
        factory, _ = _make_conn_factory(fake)
        adapter = YahooImapAdapter(
            account="yahoo_main", source_tag="[YH]", host="imap.mail.yahoo.com",
            user="me@yahoo.com", password="app-pw", conn_factory=factory,
        )

        adapter.search("from:vendor")

        self.assertNotIn("Bulk Mail", fake.select_calls)
        self.assertGreaterEqual(len(fake.select_calls), 1)


class ImapAdapterIsAConcreteMailAdapterTest(unittest.TestCase):
    """§S2 groundwork: `ImapAdapter`, `GmailImapAdapter`, `YahooImapAdapter` are
    concrete (instantiable) subclasses per the DN's "hybrid protocol" design."""

    def test_imap_adapter_subclasses_are_instantiable_with_the_documented_signature(self):
        fake = FakeIMAP()
        factory, _ = _make_conn_factory(fake)

        base = ImapAdapter(
            account="generic_main", source_tag="[IM]", host="imap.example.com",
            user="me@example.com", password="pw", port=993, conn_factory=factory,
        )

        self.assertEqual(base.account, "generic_main")
        self.assertEqual(base.source_tag, "[IM]")


if __name__ == "__main__":
    unittest.main()
