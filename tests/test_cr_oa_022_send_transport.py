"""CR-OA-022 §S1 — send transport + `send` capability (RED).

`vidushi_oa/mail/imap.py` and `vidushi_oa/mail/jmap.py` do not yet implement any
send subsystem, so every test below fails today for one of two legitimate
"send subsystem absent" reasons:

  - a behavioural assertion fails outright (e.g. `capabilities()` does not yet
    include `"send"`, or `create_draft`/`send_draft` do not exist -> AttributeError), or
  - a `mock.patch` target resolution fails because `vidushi_oa.mail.imap` does
    not yet `import smtplib` at module scope (no SMTP submission wired up yet).

Pinned shapes for GREEN (per CR-OA-022 §S1 + the DN §Decision 7 design):

  - Every adapter (`GmailImapAdapter`, `YahooImapAdapter`, `JmapAdapter`) gains
    `create_draft(raw_rfc822: bytes, folder="Drafts") -> draft_id` and
    `send_draft(draft_id) -> message_id`, and its `capabilities()` set gains the
    flag `"send"`.
  - Gmail/Yahoo (`ImapAdapter` subclasses): `create_draft` issues a single IMAP
    `APPEND` (`conn.append(mailbox, flags, date_time, message)`) to the given
    folder (default "Drafts") with the `\\Draft` flag, and returns a draft id
    derived from the APPEND response. `send_draft` submits via SMTP: it imports
    the stdlib `smtplib` module at `vidushi_oa.mail.imap` module scope (so tests
    can `mock.patch("vidushi_oa.mail.imap.smtplib.SMTP", ...)`), constructs an
    `smtplib.SMTP` instance, calls `.starttls()`, authenticates with
    `.login(self.user, self.password)` (the adapter's own stored IMAP
    credential — DN §Decision 7 revisits Decision 3: the app-password already
    authorizes SMTP), and calls `.sendmail(...)` EXACTLY ONCE.
  - Fastmail (`JmapAdapter`): `create_draft` issues one `Email/set` call whose
    `create` object carries the `$draft` keyword and returns the created
    email's id; `send_draft` issues exactly one `EmailSubmission/set` call
    whose `create` object's `emailId` references that draft's email id.

No real network / no real SMTP or IMAP connection anywhere in this file — SMTP
is faked via `mock.patch` substituting a `FakeSMTP` for `smtplib.SMTP`; IMAP is
faked via the same `FakeIMAP`-style in-file fake used by the CR-OA-020 adapter
tests (extended here with `.append()`); JMAP is faked via the same
`FakeTransport` pattern used by `tests/test_cr_oa_020_jmap.py`.
"""
import unittest
from unittest.mock import patch

from vidushi_oa.mail.imap import GmailImapAdapter, YahooImapAdapter
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


def _build_raw_message(subject, from_addr, to_addr, body):
    """A minimal, real RFC 5322 byte payload (not the §S2 `compose()` — that's
    a separate cycle) — good enough to drive the transport-layer tests here."""
    return (
        f"From: {from_addr}\r\n"
        f"To: {to_addr}\r\n"
        f"Subject: {subject}\r\n"
        f"\r\n"
        f"{body}\r\n"
    ).encode("utf-8")


class FakeSMTP:
    """Records every `.starttls()` / `.login()` / `.sendmail()` call — no real
    socket. Constructor-call args are recorded on the class-level factory mock
    (via `mock.patch(..., return_value=...)`), not here."""

    def __init__(self):
        self.starttls_calls = []
        self.login_calls = []
        self.sendmail_calls = []
        self.quit_calls = 0

    def starttls(self, context=None):
        self.starttls_calls.append(context)

    def login(self, user, password):
        self.login_calls.append((user, password))

    def sendmail(self, from_addr, to_addrs, msg, *args, **kwargs):
        self.sendmail_calls.append((from_addr, to_addrs, msg))
        return {}

    def quit(self):
        self.quit_calls += 1


class FakeImapConn:
    """A minimal fake IMAP connection covering only what the §S1 send-path
    tests need: `login`, `select`, and `append` (recording every call), plus a
    canned `append` response shaped like real `imaplib.IMAP4.append()`."""

    def __init__(self, append_response=None):
        self.append_response = append_response or ("OK", [b"[APPENDUID 1 900] (Success)"])
        self.login_calls = []
        self.select_calls = []
        self.append_calls = []

    def login(self, user, password):
        self.login_calls.append((user, password))
        return ("OK", [b"Logged in"])

    def select(self, mailbox="INBOX", readonly=False):
        self.select_calls.append(mailbox)
        return ("OK", [b"1"])

    def append(self, mailbox, flags, date_time, message):
        self.append_calls.append((mailbox, flags, date_time, message))
        return self.append_response


def _make_conn_factory(fake):
    calls = []

    def factory(host, port):
        calls.append((host, port))
        return fake

    return factory, calls


class GmailCreateDraftImapAppendTest(unittest.TestCase):
    """§S1 scope: Gmail's `create_draft` does an IMAP `APPEND` to Drafts."""

    def setUp(self):
        self.fake = FakeImapConn()
        self.factory, _ = _make_conn_factory(self.fake)
        self.adapter = GmailImapAdapter(
            account="gmail_main", source_tag="[GM]", host="imap.gmail.com",
            user="me@gmail.com", password="app-pw", conn_factory=self.factory,
        )

    def test_create_draft_appends_the_raw_message_to_drafts_and_returns_a_draft_id(self):
        raw = _build_raw_message("Draft subject", "me@gmail.com", "v@example.com", "Body text")

        draft_id = self.adapter.create_draft(raw)

        self.assertEqual(len(self.fake.append_calls), 1)
        mailbox, flags, date_time, message = self.fake.append_calls[0]
        self.assertEqual(mailbox, "Drafts")
        self.assertEqual(message, raw)
        self.assertTrue(draft_id, "create_draft must return a non-empty draft id")

    def test_create_draft_marks_the_appended_message_as_a_draft(self):
        raw = _build_raw_message("Draft subject", "me@gmail.com", "v@example.com", "Body text")

        self.adapter.create_draft(raw)

        _, flags, _, _ = self.fake.append_calls[0]
        flags_text = flags.decode() if isinstance(flags, bytes) else str(flags)
        self.assertIn("Draft", flags_text)

    def test_create_draft_honours_a_custom_folder_argument(self):
        raw = _build_raw_message("Draft subject", "me@gmail.com", "v@example.com", "Body text")

        self.adapter.create_draft(raw, folder="[Gmail]/Drafts")

        mailbox = self.fake.append_calls[0][0]
        self.assertEqual(mailbox, "[Gmail]/Drafts")

    def test_create_draft_issues_exactly_one_append_no_matter_the_message_size(self):
        raw = _build_raw_message("Another draft", "me@gmail.com", "v@example.com", "x" * 500)

        self.adapter.create_draft(raw)

        self.assertEqual(len(self.fake.append_calls), 1)


class GmailSendDraftSmtpTest(unittest.TestCase):
    """§S1 AC: against a fake SMTP server, the Gmail adapter's `send_draft`
    connects with submission + STARTTLS, authenticates with the account
    credential, and issues exactly one `sendmail`."""

    def setUp(self):
        self.fake_imap = FakeImapConn()
        self.factory, _ = _make_conn_factory(self.fake_imap)
        self.adapter = GmailImapAdapter(
            account="gmail_main", source_tag="[GM]", host="imap.gmail.com",
            user="me@gmail.com", password="app-pw", conn_factory=self.factory,
        )

    def test_send_draft_sends_exactly_once_via_starttls_submission(self):
        fake_smtp = FakeSMTP()
        with patch("vidushi_oa.mail.imap.smtplib.SMTP", return_value=fake_smtp):
            message_id = self.adapter.send_draft("900")

        self.assertEqual(len(fake_smtp.sendmail_calls), 1)
        self.assertEqual(len(fake_smtp.starttls_calls), 1)
        self.assertTrue(message_id, "send_draft must return a non-empty message id")

    def test_send_draft_authenticates_with_the_adapters_own_credential(self):
        fake_smtp = FakeSMTP()
        with patch("vidushi_oa.mail.imap.smtplib.SMTP", return_value=fake_smtp):
            self.adapter.send_draft("900")

        self.assertEqual(fake_smtp.login_calls, [("me@gmail.com", "app-pw")])


class YahooSendDraftSmtpTest(unittest.TestCase):
    """§S1 AC: the same submission contract holds for the Yahoo adapter."""

    def setUp(self):
        self.fake_imap = FakeImapConn()
        self.factory, _ = _make_conn_factory(self.fake_imap)
        self.adapter = YahooImapAdapter(
            account="yahoo_main", source_tag="[YH]", host="imap.mail.yahoo.com",
            user="me@yahoo.com", password="app-pw", conn_factory=self.factory,
        )

    def test_send_draft_sends_exactly_once_and_authenticates_with_credential(self):
        fake_smtp = FakeSMTP()
        with patch("vidushi_oa.mail.imap.smtplib.SMTP", return_value=fake_smtp):
            message_id = self.adapter.send_draft("901")

        self.assertEqual(len(fake_smtp.sendmail_calls), 1)
        self.assertEqual(len(fake_smtp.starttls_calls), 1)
        self.assertEqual(fake_smtp.login_calls, [("me@yahoo.com", "app-pw")])
        self.assertTrue(message_id)


class AdapterCapabilitiesIncludeSendTest(unittest.TestCase):
    """§S1 scope: every adapter's `capabilities()` gains the `"send"` flag."""

    def test_gmail_capabilities_include_send(self):
        fake = FakeImapConn()
        factory, _ = _make_conn_factory(fake)
        adapter = GmailImapAdapter(
            account="gmail_main", source_tag="[GM]", host="imap.gmail.com",
            user="me@gmail.com", password="app-pw", conn_factory=factory,
        )

        self.assertIn("send", adapter.capabilities())

    def test_yahoo_capabilities_include_send(self):
        fake = FakeImapConn()
        factory, _ = _make_conn_factory(fake)
        adapter = YahooImapAdapter(
            account="yahoo_main", source_tag="[YH]", host="imap.mail.yahoo.com",
            user="me@yahoo.com", password="app-pw", conn_factory=factory,
        )

        self.assertIn("send", adapter.capabilities())

    def test_jmap_capabilities_include_send(self):
        transport = _JmapFakeTransport()
        adapter = JmapAdapter(
            account="fastmail_main", source_tag="[FM]", token="secret-token",
            session_url=SESSION_URL, transport=transport,
        )

        self.assertIn("send", adapter.capabilities())


class _JmapFakeTransport:
    """Records every `(method, url, headers, body)` call and returns canned
    `(status, dict)` tuples — no network. Distinct from
    `tests/test_cr_oa_020_jmap.py`'s `FakeTransport` so each RED file stays
    import-self-contained, but returns compatible session/response shapes."""

    def __init__(self, session_response=None, responses=None):
        self.session_response = session_response or _CANNED_SESSION
        # A queue of canned POST responses, consumed in order; the last one
        # repeats once exhausted so a test needn't enumerate every call.
        self._responses = list(responses or [])
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        if method == "GET":
            return 200, self.session_response
        if self._responses:
            response = self._responses.pop(0)
        elif self.calls:
            response = {"methodResponses": []}
        else:
            response = {"methodResponses": []}
        return 200, response

    def calls_of(self, method):
        return [c for c in self.calls if c[0] == method]

    def post_bodies(self):
        return [c[3] for c in self.calls if c[0] == "POST"]


class JmapCreateDraftEmailSetTest(unittest.TestCase):
    """§S1 AC: `JmapAdapter.create_draft` issues one `Email/set` whose
    `create` object carries the `$draft` keyword, and returns the created
    email's id."""

    def test_create_draft_issues_email_set_with_draft_keyword(self):
        create_response = {
            "methodResponses": [
                ["Email/set", {"accountId": ACCOUNT_ID,
                               "created": {"draft1": {"id": "Md-draft-1"}}}, "0"],
            ],
        }
        transport = _JmapFakeTransport(responses=[create_response])
        adapter = JmapAdapter(
            account="fastmail_main", source_tag="[FM]", token="secret-token",
            session_url=SESSION_URL, transport=transport,
        )
        raw = _build_raw_message("Draft subject", "me@fastmail.com", "v@example.com", "Body")

        draft_id = adapter.create_draft(raw)

        post_bodies = transport.post_bodies()
        self.assertEqual(len(post_bodies), 1)
        method_calls = post_bodies[0]["methodCalls"]
        set_call = next(c for c in method_calls if c[0] == "Email/set")
        created_objects = list(set_call[1]["create"].values())
        self.assertEqual(len(created_objects), 1)
        keywords = created_objects[0].get("keywords", {})
        self.assertTrue(keywords.get("$draft"), f"created email must carry $draft: {created_objects[0]!r}")
        self.assertEqual(draft_id, "Md-draft-1")


class JmapSendDraftEmailSubmissionTest(unittest.TestCase):
    """§S1 AC: `JmapAdapter.send_draft` issues exactly ONE
    `EmailSubmission/set` referencing the draft's email id."""

    def test_send_draft_issues_exactly_one_email_submission_set_referencing_the_draft(self):
        submission_response = {
            "methodResponses": [
                ["EmailSubmission/set",
                 {"accountId": ACCOUNT_ID,
                  "created": {"sub1": {"id": "S-sent-1"}}}, "0"],
            ],
        }
        transport = _JmapFakeTransport(responses=[submission_response])
        adapter = JmapAdapter(
            account="fastmail_main", source_tag="[FM]", token="secret-token",
            session_url=SESSION_URL, transport=transport,
        )

        message_id = adapter.send_draft("Md-draft-1")

        post_bodies = transport.post_bodies()
        self.assertEqual(len(post_bodies), 1)
        method_calls = post_bodies[0]["methodCalls"]
        submission_calls = [c for c in method_calls if c[0] == "EmailSubmission/set"]
        self.assertEqual(len(submission_calls), 1)
        created_objects = list(submission_calls[0][1]["create"].values())
        self.assertEqual(len(created_objects), 1)
        self.assertEqual(created_objects[0].get("emailId"), "Md-draft-1")
        self.assertTrue(message_id)


if __name__ == "__main__":
    unittest.main()
