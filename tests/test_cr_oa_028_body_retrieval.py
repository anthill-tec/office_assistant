"""CR-OA-028 §S1 — in-engine HTML body retrieval (RED).

Neither `vidushi_oa/mail/imap.py` nor `vidushi_oa/mail/jmap.py` has a body-fetch path
today — `ImapAdapter`/`GmailImapAdapter`/`YahooImapAdapter` have no `fetch_html_body`
method at all (calling it is an `AttributeError`), and `JmapAdapter.fetch_message`
raises `NotImplementedError` (`jmap.py:186-187`) with no `fetch_html_body` either. So
every behavioural test below fails today for one of two legitimate "§S1 body
retrieval absent" reasons: an `AttributeError` (method doesn't exist yet) or an
outright `NotImplementedError`.

Pinned shapes for GREEN (per the CR-OA-028 §S1 scope + dispatch design):

  - `ImapAdapter.fetch_html_body(uid, folder=None) -> str | None` — reuses the
    CR-022 `_fetch_draft_bytes` FETCH-tuple pattern: `conn.select(folder or "INBOX")`
    + `conn.uid("FETCH", uid, "(BODY[])")`, then `email.message_from_bytes(raw)` and
    a MIME walk returning the decoded `text/html` part as a `str` (or `None` when the
    message carries no html part, e.g. a plain-text-only message).
  - `JmapAdapter.fetch_html_body(uid, folder=None) -> str | None` — one `Email/get`
    call requesting `properties: ["htmlBody", "bodyValues"]` for `ids: [uid]`; the
    html string is resolved from the returned item's `bodyValues[<partId>].value`
    where `<partId>` is `htmlBody[0]["partId"]` (or `None` when `htmlBody` is empty).
  - `JmapAdapter.fetch_message(uid, folder=None) -> Message` — no longer raises
    `NotImplementedError`; issues an `Email/get` for `ids: [uid]` requesting the
    header properties and returns a normal `Message` (mirrors `_build_message`).
  - §S1's other AC: the retrieved raw HTML body must never appear in `_mail_row()`'s
    AXI projection — verified here as a guard (some of it already holds true today
    because `Message`/`_mail_row` carry no body field; kept as the regression guard
    per the CR-028 dispatch, alongside an end-to-end check that a body fetched via
    the new extraction-only path never leaks into that same projection).

No real IMAP/network/HTTP anywhere in this file — IMAP is faked via an in-file
`FakeImapConn` (the CR-022 `_fetch_draft_bytes` UID-FETCH-tuple shape, extended with
real multipart/plain-text message bytes built with `email.mime`); JMAP is faked via
an in-file `_JmapFakeBodyTransport` (the CR-OA-020 `FakeTransport` GET-session /
POST-methodCalls pattern).
"""
import json
import unittest
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import vidushi_oa._cli as cli
from vidushi_oa.mail.base import Message
from vidushi_oa.mail.imap import GmailImapAdapter
from vidushi_oa.mail.jmap import JmapAdapter

SESSION_URL = "https://api.fastmail.com/jmap/session"
API_URL = "https://api.fastmail.com/jmap/api/"
ACCOUNT_ID = "u1234567"

_CANNED_SESSION = {
    "apiUrl": API_URL,
    "accounts": {
        ACCOUNT_ID: {
            "name": "new.book1604@fastmail.com",
            "isPersonal": True,
            "accountCapabilities": {"urn:ietf:params:jmap:mail": {}},
        },
    },
    "primaryAccounts": {"urn:ietf:params:jmap:mail": ACCOUNT_ID},
    "capabilities": {"urn:ietf:params:jmap:mail": {}},
}


def _build_multipart_message(subject, from_addr, to_addr, plain_text, html_text):
    """A real RFC 5322 multipart/alternative message (plain + html parts), as
    real bytes off the wire — not a hand-rolled fixture."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Message-ID"] = "<mp-body@example.com>"
    msg.attach(MIMEText(plain_text, "plain"))
    msg.attach(MIMEText(html_text, "html"))
    return msg.as_bytes()


def _build_plain_message(subject, from_addr, to_addr, body_text):
    """A real, non-multipart plain-text-only message (no html part at all)."""
    msg = MIMEText(body_text, "plain")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Message-ID"] = "<plain-only@example.com>"
    return msg.as_bytes()


class FakeImapConn:
    """A minimal fake IMAP connection covering only what the §S1 body-fetch tests
    need: `login`, `select`, and a UID `FETCH` (recording every call), returning a
    canned `(descriptor, raw_bytes)` tuple shaped like real `imaplib.IMAP4.uid()` —
    the same shape `_fetch_draft_bytes`'s CR-022 fake already uses."""

    def __init__(self, fetch_body=None):
        self.fetch_body = fetch_body
        self.login_calls = []
        self.select_calls = []
        self.uid_calls = []

    def login(self, user, password):
        self.login_calls.append((user, password))
        return ("OK", [b"Logged in"])

    def select(self, mailbox="INBOX", readonly=False):
        self.select_calls.append(mailbox)
        return ("OK", [b"1"])

    def uid(self, command, *args):
        self.uid_calls.append((command, args))
        if command == "FETCH":
            uid = args[0]
            return ("OK", [(f"{uid} (BODY[] {{{len(self.fetch_body)}}}".encode(),
                            self.fetch_body), b")"])
        return ("OK", [b""])


def _make_conn_factory(fake):
    calls = []

    def factory(host, port):
        calls.append((host, port))
        return fake

    return factory, calls


class _JmapFakeBodyTransport:
    """Records every `(method, url, headers, body)` call and returns canned
    `(status, dict)` tuples — no network. A canned GET session response, then a
    queue of canned POST `methodResponses` bodies consumed in order."""

    def __init__(self, session_response=None, responses=None):
        self.session_response = session_response or _CANNED_SESSION
        self._responses = list(responses or [])
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        if method == "GET":
            return 200, self.session_response
        if self._responses:
            response = self._responses.pop(0)
        else:
            response = {"methodResponses": []}
        return 200, response

    def calls_of(self, method):
        return [c for c in self.calls if c[0] == method]

    def post_bodies(self):
        return [c[3] for c in self.calls if c[0] == "POST"]


class ImapFetchHtmlBodyMultipartTest(unittest.TestCase):
    """§S1 AC: the IMAP adapter can fetch a message's `text/html` part, asserted
    against a fake conn returning a multipart (plain + html) body."""

    def setUp(self):
        self.html_content = "<html><body><p>Your order 12345 has shipped.</p></body></html>"
        self.plain_content = "Your order 12345 has shipped."
        self.multipart_bytes = _build_multipart_message(
            "Shipping update", "orders@shop.example", "me@gmail.com",
            self.plain_content, self.html_content,
        )
        self.fake = FakeImapConn(fetch_body=self.multipart_bytes)
        self.factory, _ = _make_conn_factory(self.fake)
        self.adapter = GmailImapAdapter(
            account="gmail_main", source_tag="[GM]", host="imap.gmail.com",
            user="me@gmail.com", password="app-pw", conn_factory=self.factory,
        )

    def test_fetch_html_body_returns_the_decoded_html_part_of_a_multipart_message(self):
        html = self.adapter.fetch_html_body("500")

        self.assertIsInstance(html, str)
        self.assertIn("Your order 12345 has shipped.", html)
        self.assertIn("<p>", html, "must be the HTML part, not the plain-text part")
        self.assertNotEqual(html.strip(), self.plain_content)

    def test_fetch_html_body_issues_a_uid_fetch_with_the_body_spec_for_the_given_uid(self):
        self.adapter.fetch_html_body("500")

        fetch_calls = [c for c in self.fake.uid_calls if c[0] == "FETCH"]
        self.assertEqual(len(fetch_calls), 1, "must issue exactly one UID FETCH")
        _, args = fetch_calls[0]
        self.assertEqual(args[0], "500")
        joined = " ".join(str(a) for a in args).upper()
        self.assertIn("BODY[]", joined)

    def test_fetch_html_body_selects_the_default_inbox_folder(self):
        self.adapter.fetch_html_body("500")

        self.assertIn("INBOX", self.fake.select_calls)

    def test_fetch_html_body_honours_a_custom_folder_argument(self):
        self.adapter.fetch_html_body("500", folder="Archive")

        self.assertIn("Archive", self.fake.select_calls)


class ImapFetchHtmlBodyPlainTextOnlyTest(unittest.TestCase):
    """§S1 edge case: a plain-text-only message (no html part at all) must not
    fabricate an html string — `fetch_html_body` returns `None`."""

    def test_plain_text_only_message_returns_none(self):
        plain_bytes = _build_plain_message(
            "Receipt", "billing@shop.example", "me@gmail.com", "Thanks for your order.")
        fake = FakeImapConn(fetch_body=plain_bytes)
        factory, _ = _make_conn_factory(fake)
        adapter = GmailImapAdapter(
            account="gmail_main", source_tag="[GM]", host="imap.gmail.com",
            user="me@gmail.com", password="app-pw", conn_factory=factory,
        )

        html = adapter.fetch_html_body("501")

        self.assertIsNone(html, "a plain-text-only message must yield None, not a fabricated string")


class JmapFetchHtmlBodyTest(unittest.TestCase):
    """§S1 AC: the JMAP adapter's `fetch_html_body` requests `htmlBody`/`bodyValues`
    via `Email/get` (asserted against a fake transport) and returns the html string."""

    def test_fetch_html_body_requests_html_body_and_body_values_via_email_get(self):
        html_content = "<html><body><p>Your parcel is on its way.</p></body></html>"
        response = {
            "methodResponses": [
                ["Email/get",
                 {"accountId": ACCOUNT_ID,
                  "list": [{
                      "id": "Ma1",
                      "htmlBody": [{"partId": "htmlpart1", "type": "text/html"}],
                      "bodyValues": {"htmlpart1": {"value": html_content}},
                  }],
                  "notFound": []},
                 "0"],
            ],
        }
        transport = _JmapFakeBodyTransport(responses=[response])
        adapter = JmapAdapter(
            account="fastmail_main", source_tag="[FM]", token="secret-token",
            session_url=SESSION_URL, transport=transport,
        )

        html = adapter.fetch_html_body("Ma1")

        self.assertEqual(html, html_content)
        post_bodies = transport.post_bodies()
        self.assertEqual(len(post_bodies), 1, "must issue exactly one POST")
        get_call = next(c for c in post_bodies[0]["methodCalls"] if c[0] == "Email/get")
        self.assertEqual(get_call[1]["accountId"], ACCOUNT_ID)
        self.assertIn("Ma1", get_call[1].get("ids", []))
        properties = get_call[1].get("properties", [])
        self.assertIn("htmlBody", properties)
        self.assertIn("bodyValues", properties)

    def test_fetch_html_body_returns_none_when_no_html_body_part_is_present(self):
        response = {
            "methodResponses": [
                ["Email/get",
                 {"accountId": ACCOUNT_ID,
                  "list": [{"id": "Ma2", "htmlBody": [], "bodyValues": {}}],
                  "notFound": []},
                 "0"],
            ],
        }
        transport = _JmapFakeBodyTransport(responses=[response])
        adapter = JmapAdapter(
            account="fastmail_main", source_tag="[FM]", token="secret-token",
            session_url=SESSION_URL, transport=transport,
        )

        html = adapter.fetch_html_body("Ma2")

        self.assertIsNone(html, "no htmlBody part must yield None, not a fabricated string")


class JmapFetchMessageImplementedTest(unittest.TestCase):
    """§S1 AC: `JmapAdapter.fetch_message` is implemented (no longer
    `NotImplementedError`) — it issues an `Email/get` for the given id and returns
    a real `Message`."""

    def test_fetch_message_returns_a_message_built_from_the_email_get_response(self):
        response = {
            "methodResponses": [
                ["Email/get",
                 {"accountId": ACCOUNT_ID,
                  "list": [{
                      "id": "Ma1",
                      "threadId": "Ta1",
                      "messageId": ["<msg1@example.com>"],
                      "subject": "Invoice from Acme",
                      "from": [{"name": "Acme Billing", "email": "billing@acme.example"}],
                      "to": [{"name": "Antony John", "email": "new.book1604@fastmail.com"}],
                      "receivedAt": "2026-07-20T10:00:00Z",
                  }],
                  "notFound": []},
                 "0"],
            ],
        }
        transport = _JmapFakeBodyTransport(responses=[response])
        adapter = JmapAdapter(
            account="fastmail_main", source_tag="[FM]", token="secret-token",
            session_url=SESSION_URL, transport=transport,
        )

        msg = adapter.fetch_message("Ma1")

        self.assertIsInstance(msg, Message)
        self.assertEqual(msg.id, "<msg1@example.com>")
        self.assertEqual(msg.subject, "Invoice from Acme")
        self.assertIn("billing@acme.example", msg.sender)
        self.assertEqual(msg.account, "fastmail_main")
        self.assertEqual(msg.source_tag, "[FM]")
        self.assertEqual(msg.thread_id, "Ta1")

    def test_fetch_message_issues_exactly_one_email_get_for_the_given_id(self):
        response = {
            "methodResponses": [
                ["Email/get",
                 {"accountId": ACCOUNT_ID, "list": [], "notFound": ["Ma1"]},
                 "0"],
            ],
        }
        transport = _JmapFakeBodyTransport(responses=[response])
        adapter = JmapAdapter(
            account="fastmail_main", source_tag="[FM]", token="secret-token",
            session_url=SESSION_URL, transport=transport,
        )

        adapter.fetch_message("Ma1")

        post_bodies = transport.post_bodies()
        self.assertEqual(len(post_bodies), 1, "must issue exactly one POST")
        get_calls = [c for c in post_bodies[0]["methodCalls"] if c[0] == "Email/get"]
        self.assertEqual(len(get_calls), 1)
        self.assertIn("Ma1", get_calls[0][1].get("ids", []))
        self.assertEqual(get_calls[0][1]["accountId"], ACCOUNT_ID)


class BodyNeverSurfacedTest(unittest.TestCase):
    """§S1 AC: the retrieved raw body is not present in `mail-search`/`mail-get`
    output nor in the AXI mail row — only structured fields leave the engine."""

    def test_mail_row_projection_carries_no_body_or_html_field(self):
        """Guard: `_mail_row()`'s fixed projection has no body/html key at all,
        for a Message shaped exactly like a real mail-search/mail-get result."""
        msg = Message(
            id="<order-99@example.com>", account="gmail_main", source_tag="[GM]",
            subject="Order confirmed", sender="orders@vendor.example",
            to="me@gmail.com", date="2026-07-20T10:00:00Z", uid="99",
        )

        row = cli._mail_row(msg)

        for forbidden_key in ("body", "html", "html_body", "raw_body"):
            self.assertNotIn(forbidden_key, row, f"AXI row must never carry {forbidden_key!r}")
        self.assertEqual(
            set(row.keys()), {"id", "uid", "account", "source_tag", "subject", "sender", "date"})

    def test_html_body_fetched_via_the_extraction_only_path_never_leaks_into_the_mail_row(self):
        """End-to-end guard: fetch the real html body through the new §S1 path,
        then confirm the AXI row built for the SAME message carries none of its
        content — proving the extraction-only fetch and the AXI projection are
        genuinely separate paths, not a shared body field on Message."""
        html_content = "<html><body><p>Confidential order details 12345</p></body></html>"
        plain_content = "Confidential order details 12345"
        multipart_bytes = _build_multipart_message(
            "Order details", "orders@shop.example", "me@gmail.com",
            plain_content, html_content,
        )
        fake = FakeImapConn(fetch_body=multipart_bytes)
        factory, _ = _make_conn_factory(fake)
        adapter = GmailImapAdapter(
            account="gmail_main", source_tag="[GM]", host="imap.gmail.com",
            user="me@gmail.com", password="app-pw", conn_factory=factory,
        )

        html = adapter.fetch_html_body("700")
        msg = Message(
            id="<order-700@example.com>", account="gmail_main", source_tag="[GM]",
            subject="Order details", sender="orders@shop.example",
            to="me@gmail.com", date="2026-07-20T10:00:00Z", uid="700",
        )
        row = cli._mail_row(msg)

        # Sanity: the body really was fetched (this must not be vacuously true).
        self.assertIn("Confidential order details 12345", html)
        row_text = json.dumps(row)
        self.assertNotIn("Confidential order details 12345", row_text)
        self.assertNotIn("<p>", row_text)


if __name__ == "__main__":
    unittest.main()
