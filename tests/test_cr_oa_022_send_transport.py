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
  - Fastmail (`JmapAdapter`): `create_draft` uploads the literal `raw_rfc822`
    bytes as a blob to the session `uploadUrl` and issues one `Email/import`
    referencing that `blobId` and the resolved `drafts`-role mailbox, with the
    `$draft` keyword, returning the created email's id (superseding the original
    content-less `Email/set` shape, which created EMPTY Fastmail drafts);
    `send_draft` issues exactly one `EmailSubmission/set` call whose `create`
    object's `emailId` references that draft's email id, with an
    `onSuccessUpdateEmail` patch clearing `$draft` and filing the message in the
    `sent`-role mailbox.

No real network / no real SMTP or IMAP connection anywhere in this file — SMTP
is faked via `mock.patch` substituting a `FakeSMTP` for `smtplib.SMTP`; IMAP is
faked via the same `FakeIMAP`-style in-file fake used by the CR-OA-020 adapter
tests (extended here with `.append()`); JMAP is faked via the same
`FakeTransport` pattern used by `tests/test_cr_oa_020_jmap.py`.
"""
import imaplib
import smtplib
import unittest
from unittest.mock import patch

from vidushi_oa.mail.compose import compose
from vidushi_oa.mail.imap import GmailImapAdapter, YahooImapAdapter
from vidushi_oa.mail.jmap import JmapAdapter
from vidushi_oa.mail.xoauth2 import GmailXoauth2Adapter

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

UPLOAD_URL_TEMPLATE = "https://api.fastmail.com/upload/{accountId}/"
UPLOAD_URL = f"https://api.fastmail.com/upload/{ACCOUNT_ID}/"
DRAFTS_MAILBOX_ID = "Mb-drafts"
BLOB_ID = "Gb-blob-1"

# Every real Fastmail session advertises `uploadUrl` (RFC 8620 §2 makes it a
# mandatory Session property) — the blob+import draft flow is the only path.
_SESSION_WITH_UPLOAD = dict(_CANNED_SESSION, uploadUrl=UPLOAD_URL_TEMPLATE)

SENT_MAILBOX_ID = "Mb-sent"

_MAILBOX_QUERY_OK = {
    "methodResponses": [
        ["Mailbox/query", {"accountId": ACCOUNT_ID, "ids": [DRAFTS_MAILBOX_ID]}, "0"],
    ],
}
_SENT_MAILBOX_QUERY_OK = {
    "methodResponses": [
        ["Mailbox/query", {"accountId": ACCOUNT_ID, "ids": [SENT_MAILBOX_ID]}, "0"],
    ],
}
_SUBMISSION_OK = {
    "methodResponses": [
        ["EmailSubmission/set",
         {"accountId": ACCOUNT_ID, "created": {"submission": {"id": "S-sent-1"}}}, "0"],
    ],
}
_IMPORT_OK = {
    "methodResponses": [
        ["Email/import",
         {"accountId": ACCOUNT_ID, "created": {"draft": {"id": "Md-draft-1"}}}, "0"],
    ],
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
    """Records every `.starttls()` / `.login()` / `.auth()` / `.sendmail()` /
    `.quit()` call — no real socket. Constructor-call args are recorded on the
    class-level factory mock (via `mock.patch(..., return_value=...)`), not here.

    `auth` mirrors `smtplib.SMTP.auth`: it invokes the caller's `authobject` with
    NO argument for the initial response and records the string it hands back —
    the mechanism-specific SASL payload smtplib base64-encodes itself.

    `sendmail_error`/`quit_error`, when set, are raised instead — a submission that
    fails must still close its connection, and a `QUIT` that fails must not turn a
    delivered message into an error."""

    def __init__(self, sendmail_error=None, quit_error=None):
        self.starttls_calls = []
        self.login_calls = []
        self.auth_calls = []
        self.sendmail_calls = []
        self.quit_calls = 0
        self.sendmail_error = sendmail_error
        self.quit_error = quit_error

    def starttls(self, context=None):
        self.starttls_calls.append(context)

    def login(self, user, password):
        self.login_calls.append((user, password))

    def auth(self, mechanism, authobject, *, initial_response_ok=True):
        self.auth_calls.append((mechanism, authobject()))
        return (235, b"2.7.0 Accepted")

    def sendmail(self, from_addr, to_addrs, msg, *args, **kwargs):
        if self.sendmail_error is not None:
            raise self.sendmail_error
        self.sendmail_calls.append((from_addr, to_addrs, msg))
        return {}

    def quit(self):
        self.quit_calls += 1
        if self.quit_error is not None:
            raise self.quit_error


class FakeImapConn:
    """A minimal fake IMAP connection covering only what the §S1 send-path
    tests need: `login`, `select`, `append`, and a UID `FETCH` (recording every
    call), plus a canned `append` response shaped like real
    `imaplib.IMAP4.append()`. `fetch_body` is the raw draft bytes returned by a
    `conn.uid("FETCH", uid, "(BODY[])")` — shaped like imaplib's
    `(descriptor, raw_bytes)` tuple item — so `send_draft` reads back a real
    stored draft rather than fabricating one.

    `list()` answers a LIST response advertising the RFC 6154 special-use
    attributes, so the Sent and Drafts mailboxes can be resolved by role rather
    than by a hard-coded name. `list_error`, when set, is raised instead — the
    mailbox bookkeeping that follows a send must never turn a delivered message
    into a failure; `list_errors` is the same thing as a queue (one entry per LIST
    call, `None` = answer normally), so a LIST that only fails AFTER delivery can
    be exercised. `list_statuses` is a queue of tagged statuses one per LIST call
    (falling back to `OK` once drained), so a refusal that later recovers can be
    exercised — imaplib returns a tagged `NO` quietly.

    `append_responses` maps a mailbox name to the tagged response its APPEND
    answers with, so a refusal (`NO [OVERQUOTA]`) can be exercised for one
    mailbox only; `store_response` does the same for the UID `STORE` calls. Both
    default to `OK` — imaplib returns a tagged `NO` quietly, so the adapter must
    read the status rather than rely on an exception."""

    _DEFAULT_LIST = [
        b'(\\HasNoChildren) "/" "INBOX"',
        b'(\\HasNoChildren \\Drafts) "/" "Drafts"',
        b'(\\HasNoChildren \\Sent) "/" "Sent"',
    ]

    def __init__(self, append_response=None, fetch_body=None, list_response=None,
                 list_error=None, append_responses=None, store_response=None,
                 list_statuses=None, list_errors=None):
        self.append_response = append_response or ("OK", [b"[APPENDUID 1 900] (Success)"])
        self.append_responses = dict(append_responses or {})
        self.store_response = store_response or ("OK", [b"Completed"])
        self.fetch_body = fetch_body
        self.list_response = list(
            self._DEFAULT_LIST if list_response is None else list_response)
        self.list_error = list_error
        self.list_errors = list(list_errors or [])
        self.list_statuses = list(list_statuses or [])
        self.login_calls = []
        self.authenticate_calls = []
        self.select_calls = []
        self.append_calls = []
        self.uid_calls = []
        self.list_calls = 0

    def login(self, user, password):
        self.login_calls.append((user, password))
        return ("OK", [b"Logged in"])

    def authenticate(self, mechanism, authobject):
        """The XOAUTH2 IMAP login (`GmailXoauth2Adapter._conn`) — recorded with the
        RAW (unencoded) SASL bytes, which is what `imaplib` encodes itself."""
        self.authenticate_calls.append((mechanism, authobject(b"")))
        return ("OK", [b"Authenticated"])

    def select(self, mailbox="INBOX", readonly=False):
        self.select_calls.append(mailbox)
        return ("OK", [b"1"])

    def append(self, mailbox, flags, date_time, message):
        self.append_calls.append((mailbox, flags, date_time, message))
        return self.append_responses.get(mailbox, self.append_response)

    def list(self, directory='""', pattern="*"):
        self.list_calls += 1
        error = self.list_errors.pop(0) if self.list_errors else self.list_error
        if error is not None:
            raise error
        status = self.list_statuses.pop(0) if self.list_statuses else "OK"
        if status != "OK":
            return (status, [b"[SERVERBUG] LIST refused"])
        return ("OK", list(self.list_response))

    def uid(self, command, *args):
        self.uid_calls.append((command, args))
        if command == "FETCH":
            uid = args[0]
            return ("OK", [(f"{uid} (BODY[] {{{len(self.fetch_body)}}}".encode(),
                            self.fetch_body), b")"])
        if command == "STORE":
            return self.store_response
        return ("OK", [b""])


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

    def test_a_refused_append_raises_instead_of_returning_a_bogus_draft_id(self):
        """imaplib raises only on a tagged `BAD` — a `NO` comes back quietly, so a
        refused APPEND would otherwise surface as a successful draft whose id is
        the server's error text (parity with the JMAP `notCreated` handling)."""
        fake = FakeImapConn(append_response=("NO", [b"[TRYCREATE] No such mailbox"]))
        factory, _ = _make_conn_factory(fake)
        adapter = GmailImapAdapter(
            account="gmail_main", source_tag="[GM]", host="imap.gmail.com",
            user="me@gmail.com", password="app-pw", conn_factory=factory,
        )
        raw = _build_raw_message("Draft subject", "me@gmail.com", "v@example.com", "Body")

        with self.assertRaises(RuntimeError) as caught:
            adapter.create_draft(raw)

        self.assertIn("TRYCREATE", str(caught.exception))


class GmailSendDraftSmtpTest(unittest.TestCase):
    """§S1 AC: against a fake SMTP server, the Gmail adapter's `send_draft`
    connects with submission + STARTTLS, authenticates with the account
    credential, and issues exactly one `sendmail`."""

    def setUp(self):
        # The real stored draft the adapter must fetch and dispatch: its To/Cc
        # are the true recipients, distinct from the account's own address, so a
        # regression to the old `sendmail(self.user, [self.user], "draft:<id>")`
        # placeholder would be caught by the assertions below.
        self.draft_bytes = compose(
            from_addr="me@gmail.com", to="vendor@example.com",
            cc="cc-team@example.com", subject="Warranty claim", body="Please assist.")
        self.fake_imap = FakeImapConn(fetch_body=self.draft_bytes)
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

    def test_send_draft_dispatches_the_real_draft_bytes_to_the_real_recipients(self):
        fake_smtp = FakeSMTP()
        with patch("vidushi_oa.mail.imap.smtplib.SMTP", return_value=fake_smtp):
            self.adapter.send_draft("900")

        # The adapter must FETCH the stored draft (BODY[]) by its UID, not fabricate one.
        fetch_cmds = [c for c in self.fake_imap.uid_calls if c[0] == "FETCH"]
        self.assertEqual(len(fetch_cmds), 1)
        self.assertEqual(fetch_cmds[0][1][0], "900")

        from_addr, recipients, msg = fake_smtp.sendmail_calls[0]
        # (a) Recipients parsed from the draft's To + Cc — NOT the account's own address.
        self.assertEqual(recipients, ["vendor@example.com", "cc-team@example.com"])
        self.assertNotIn("me@gmail.com", recipients)
        self.assertEqual(from_addr, "me@gmail.com")
        # (b) The exact stored draft bytes — NOT a `draft:<id>` placeholder.
        self.assertEqual(msg, self.draft_bytes)
        self.assertNotEqual(msg, b"draft:900")


class YahooSendDraftSmtpTest(unittest.TestCase):
    """§S1 AC: the same submission contract holds for the Yahoo adapter."""

    def setUp(self):
        self.draft_bytes = compose(
            from_addr="me@yahoo.com", to="support@example.com",
            subject="Return request", body="Requesting an RMA.")
        self.fake_imap = FakeImapConn(fetch_body=self.draft_bytes)
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

    def test_send_draft_dispatches_the_real_draft_bytes_to_the_real_recipient(self):
        fake_smtp = FakeSMTP()
        with patch("vidushi_oa.mail.imap.smtplib.SMTP", return_value=fake_smtp):
            self.adapter.send_draft("901")

        from_addr, recipients, msg = fake_smtp.sendmail_calls[0]
        self.assertEqual(recipients, ["support@example.com"])
        self.assertNotIn("me@yahoo.com", recipients)
        self.assertEqual(from_addr, "me@yahoo.com")
        self.assertEqual(msg, self.draft_bytes)
        self.assertNotEqual(msg, b"draft:901")


class GmailXoauth2SendDraftSmtpAuthTest(unittest.TestCase):
    """A Workspace account authenticating over XOAUTH2 has NO password, so the
    inherited `smtp.login(user, "")` fails SMTP AUTH on every send. Submission must
    use the `XOAUTH2` SASL mechanism with the same refreshed access token the IMAP
    side authenticates with."""

    def setUp(self):
        self.draft_bytes = compose(
            from_addr="me@workspace.example", to="vendor@example.com",
            subject="Warranty claim", body="Please assist.")
        self.fake_imap = FakeImapConn(fetch_body=self.draft_bytes)
        self.factory, _ = _make_conn_factory(self.fake_imap)
        self.token_calls = []
        self.adapter = GmailXoauth2Adapter(
            account="gmail_ws", source_tag="[GM]", host="imap.gmail.com",
            user="me@workspace.example", access_token=self._token,
            conn_factory=self.factory,
        )

    def _token(self):
        self.token_calls.append(1)
        return "access-token-123"

    def _send(self, fake_smtp, draft_id="900"):
        with patch("vidushi_oa.mail.imap.smtplib.SMTP", return_value=fake_smtp):
            return self.adapter.send_draft(draft_id)

    def test_send_draft_authenticates_over_xoauth2_not_an_empty_password_login(self):
        fake_smtp = FakeSMTP()

        message_id = self._send(fake_smtp)

        self.assertEqual(fake_smtp.login_calls, [],
                         "an XOAUTH2 account has no password — LOGIN must never be issued")
        self.assertEqual(len(fake_smtp.auth_calls), 1,
                         f"exactly one SMTP AUTH expected; got {fake_smtp.auth_calls!r}")
        self.assertEqual(fake_smtp.auth_calls[0][0], "XOAUTH2")
        self.assertEqual(len(fake_smtp.sendmail_calls), 1)
        self.assertEqual(len(fake_smtp.starttls_calls), 1)
        self.assertTrue(message_id, "send_draft must return a non-empty message id")

    def test_the_sasl_payload_carries_the_refreshed_bearer_token_unencoded(self):
        """`smtplib.SMTP.auth` base64-encodes whatever the authobject returns, so the
        RAW SASL string must be handed over — pre-encoding it (the `imaplib` form)
        would be double-encoded on the wire."""
        fake_smtp = FakeSMTP()

        self._send(fake_smtp)

        self.assertEqual(
            fake_smtp.auth_calls[0][1],
            "user=me@workspace.example\x01auth=Bearer access-token-123\x01\x01")

    def test_the_smtp_and_imap_sides_share_one_minted_token(self):
        """The token provider is the account's refresh exchange — minting a second
        one per send would double every token request."""
        self._send(FakeSMTP())

        self.assertEqual(len(self.token_calls), 1,
                         "the access-token provider must be invoked at most once")
        self.assertEqual([m for m, _sasl in self.fake_imap.authenticate_calls],
                         ["XOAUTH2"])
        self.assertEqual(
            self.fake_imap.authenticate_calls[0][1],
            b"user=me@workspace.example\x01auth=Bearer access-token-123\x01\x01",
            "the IMAP side authenticates with the same raw SASL bytes")

    def test_the_dispatched_bytes_and_recipients_are_the_stored_drafts_own(self):
        fake_smtp = FakeSMTP()

        self._send(fake_smtp)

        from_addr, recipients, msg = fake_smtp.sendmail_calls[0]
        self.assertEqual(from_addr, "me@workspace.example")
        self.assertEqual(recipients, ["vendor@example.com"])
        self.assertEqual(msg, self.draft_bytes)


class ImapSendDraftClosesTheSubmissionConnectionTest(unittest.TestCase):
    """The SMTP submission socket must be closed on every path: without a `QUIT`
    the server records an aborted session, and on a failure path the TLS socket
    leaks until garbage collection."""

    def _yahoo(self, fake):
        factory, _ = _make_conn_factory(fake)
        return YahooImapAdapter(
            account="yahoo_main", source_tag="[YH]", host="imap.mail.yahoo.com",
            user="me@yahoo.com", password="app-pw", conn_factory=factory,
        )

    def _draft_bytes(self):
        return compose(from_addr="me@yahoo.com", to="support@example.com",
                       subject="Return request", body="Requesting an RMA.")

    def test_a_delivered_send_quits_the_submission_session(self):
        fake_smtp = FakeSMTP()
        adapter = self._yahoo(FakeImapConn(fetch_body=self._draft_bytes()))

        with patch("vidushi_oa.mail.imap.smtplib.SMTP", return_value=fake_smtp):
            adapter.send_draft("901")

        self.assertEqual(fake_smtp.quit_calls, 1)

    def test_a_failed_submission_still_closes_the_connection(self):
        fake_smtp = FakeSMTP(sendmail_error=smtplib.SMTPRecipientsRefused({}))
        adapter = self._yahoo(FakeImapConn(fetch_body=self._draft_bytes()))

        with patch("vidushi_oa.mail.imap.smtplib.SMTP", return_value=fake_smtp):
            with self.assertRaises(smtplib.SMTPException):
                adapter.send_draft("901")

        self.assertEqual(fake_smtp.quit_calls, 1,
                         "a refused submission must not leak its TLS socket")

    def test_a_failing_quit_does_not_turn_a_delivered_send_into_an_error(self):
        """The message is already in the provider's hands by then — the close is
        bookkeeping, exactly like the Sent/Drafts steps that follow it."""
        fake_smtp = FakeSMTP(quit_error=smtplib.SMTPServerDisconnected("bye"))
        fake_imap = FakeImapConn(fetch_body=self._draft_bytes())
        adapter = self._yahoo(fake_imap)

        with patch("vidushi_oa.mail.imap.smtplib.SMTP", return_value=fake_smtp):
            message_id = adapter.send_draft("901")

        self.assertTrue(message_id)
        self.assertEqual([c[0] for c in fake_imap.append_calls], ["Sent"],
                         "the post-delivery bookkeeping must still run")


class ImapSendDraftFilesTheSentCopyTest(unittest.TestCase):
    """A submitted draft must stop being a draft on the IMAP path too — parity with
    the JMAP `onSuccessUpdateEmail` patch. Without it the sent message sits in
    Drafts flagged `\\Draft` forever and Sent holds no record of the outbound
    correspondence (Gmail masks this by filing SMTP submissions server-side;
    Fastmail Basic and Yahoo do not).

    Every step is bookkeeping AFTER delivery, so none of it may turn a message the
    provider already accepted into a failed send."""

    def _yahoo(self, fake):
        factory, _ = _make_conn_factory(fake)
        return YahooImapAdapter(
            account="yahoo_main", source_tag="[YH]", host="imap.mail.yahoo.com",
            user="me@yahoo.com", password="app-pw", conn_factory=factory,
        )

    def _draft_bytes(self, from_addr="me@yahoo.com"):
        return compose(from_addr=from_addr, to="support@example.com",
                       subject="Return request", body="Requesting an RMA.")

    def _send(self, adapter, draft_id="901"):
        fake_smtp = FakeSMTP()
        with patch("vidushi_oa.mail.imap.smtplib.SMTP", return_value=fake_smtp):
            message_id = adapter.send_draft(draft_id)
        return fake_smtp, message_id

    def test_the_sent_message_is_appended_to_the_special_use_sent_mailbox(self):
        raw = self._draft_bytes()
        fake = FakeImapConn(fetch_body=raw)

        self._send(self._yahoo(fake))

        appends = [c for c in fake.append_calls if c[0] == "Sent"]
        self.assertEqual(len(appends), 1,
                         f"exactly one APPEND to Sent expected; got {fake.append_calls!r}")
        self.assertEqual(appends[0][3], raw,
                         "the APPEND must carry the exact bytes that were sent")
        flags = appends[0][1]
        flags_text = flags.decode() if isinstance(flags, bytes) else str(flags or "")
        self.assertNotIn("Draft", flags_text,
                         "the filed Sent copy must not be flagged as a draft")

    def test_the_drafts_copy_stops_being_a_draft(self):
        fake = FakeImapConn(fetch_body=self._draft_bytes())

        self._send(self._yahoo(fake))

        stores = [args for command, args in fake.uid_calls if command == "STORE"]
        self.assertTrue(stores, "the Drafts copy must be retired after a send")
        for args in stores:
            self.assertEqual(args[0], "901",
                             "only the sent draft's own UID may be touched")
        flag_ops = [(args[1], args[2]) for args in stores]
        self.assertIn(("-FLAGS", r"(\Draft)"), flag_ops,
                      f"the \\Draft flag must be cleared; got {flag_ops!r}")
        self.assertIn(("+FLAGS", r"(\Deleted)"), flag_ops,
                      f"the Drafts copy must be removed; got {flag_ops!r}")
        self.assertIn("Drafts", fake.select_calls,
                      "the STOREs must be issued against the Drafts mailbox")

    def test_gmail_does_not_append_a_duplicate_sent_copy(self):
        """Gmail files every SMTP submission into Sent Mail itself — appending a
        second copy would duplicate the user's outbound record."""
        fake = FakeImapConn(fetch_body=self._draft_bytes("me@gmail.com"))
        factory, _ = _make_conn_factory(fake)
        adapter = GmailImapAdapter(
            account="gmail_main", source_tag="[GM]", host="imap.gmail.com",
            user="me@gmail.com", password="app-pw", conn_factory=factory,
        )

        self._send(adapter, "900")

        self.assertEqual(fake.append_calls, [],
                         "Gmail must not APPEND a Sent copy the server already filed")
        stores = [args for command, args in fake.uid_calls if command == "STORE"]
        self.assertTrue(stores, "the Drafts copy must still be retired")

    def test_the_filed_sent_copy_is_marked_read(self):
        """Mail clients APPEND the sent copy `\\Seen` — a message the user wrote
        must not land in Sent driving an unread badge."""
        fake = FakeImapConn(fetch_body=self._draft_bytes())

        self._send(self._yahoo(fake))

        flags = [c for c in fake.append_calls if c[0] == "Sent"][0][1]
        flags_text = flags.decode() if isinstance(flags, bytes) else str(flags or "")
        self.assertIn("Seen", flags_text)

    def test_an_unquoted_sent_mailbox_name_is_resolved_not_the_delimiter(self):
        """IMAP permits an unquoted atom for the mailbox name; naive `rsplit('"')`
        parsing returns the hierarchy delimiter instead and APPENDs to `/`."""
        fake = FakeImapConn(
            fetch_body=self._draft_bytes(),
            list_response=[b'(\\HasNoChildren) "/" INBOX',
                           b'(\\HasNoChildren \\Drafts) "/" Drafts',
                           b'(\\HasNoChildren \\Sent) "/" Sent'])

        self._send(self._yahoo(fake))

        self.assertEqual([c[0] for c in fake.append_calls], ["Sent"])

    def test_the_drafts_copy_is_expunged_by_its_own_uid_only(self):
        """A bare EXPUNGE would reap everything else the user had flagged
        `\\Deleted` in Drafts, so the removal must be a UID EXPUNGE."""
        fake = FakeImapConn(fetch_body=self._draft_bytes())

        self._send(self._yahoo(fake))

        self.assertIn(("EXPUNGE", ("901",)), fake.uid_calls)

    def test_an_account_with_no_sent_mailbox_keeps_the_drafts_copy(self):
        """No Sent copy was filed, so destroying the Drafts copy would leave the
        sent message recorded in neither folder."""
        fake = FakeImapConn(
            fetch_body=self._draft_bytes(),
            list_response=[b'(\\HasNoChildren) "/" "INBOX"',
                           b'(\\HasNoChildren \\Drafts) "/" "Drafts"'])

        _fake_smtp, message_id = self._send(self._yahoo(fake))

        self.assertTrue(message_id)
        self.assertEqual(fake.append_calls, [])
        self.assertEqual([c for c in fake.uid_calls if c[0] == "EXPUNGE"], [],
                         "the draft must survive when no Sent copy was filed")
        deletes = [args for command, args in fake.uid_calls
                   if command == "STORE" and args[1] == "+FLAGS"]
        self.assertEqual(deletes, [], "the draft must not be flagged \\Deleted")

    def test_a_refused_sent_append_keeps_the_drafts_copy(self):
        """A tagged `NO` (quota, permission, mailbox vanished) files no Sent copy —
        expunging the draft anyway would destroy the only remaining record."""
        fake = FakeImapConn(fetch_body=self._draft_bytes(),
                            append_responses={"Sent": ("NO", [b"[OVERQUOTA] Over quota"])})

        _fake_smtp, message_id = self._send(self._yahoo(fake))

        self.assertTrue(message_id, "a delivered message must still report its id")
        self.assertEqual([c for c in fake.uid_calls if c[0] == "EXPUNGE"], [],
                         "a refused Sent APPEND must not expunge the draft")

    def test_a_refused_delete_flag_store_does_not_expunge(self):
        fake = FakeImapConn(fetch_body=self._draft_bytes(),
                            store_response=("NO", [b"Permission denied"]))

        _fake_smtp, message_id = self._send(self._yahoo(fake))

        self.assertTrue(message_id)
        self.assertEqual([c for c in fake.uid_calls if c[0] == "EXPUNGE"], [])

    def test_a_refused_delete_flag_store_leaves_the_draft_keyword_set(self):
        """On the retain path the draft must be left exactly as it was: clearing
        `\\Draft` first would strand a non-draft in Drafts that no client offers to
        resume, while a Sent copy of it already exists."""
        fake = FakeImapConn(fetch_body=self._draft_bytes(),
                            store_response=("NO", [b"Permission denied"]))

        self._send(self._yahoo(fake))

        flag_ops = [(args[1], args[2]) for command, args in fake.uid_calls
                    if command == "STORE"]
        self.assertNotIn(("-FLAGS", r"(\Draft)"), flag_ops,
                         f"the retained draft must keep its \\Draft flag; got {flag_ops!r}")

    def test_gmail_still_expunges_the_draft_the_server_filed_itself(self):
        """Gmail files the Sent copy server-side, so the safety gate is satisfied
        without an APPEND of our own."""
        fake = FakeImapConn(fetch_body=self._draft_bytes("me@gmail.com"))
        factory, _ = _make_conn_factory(fake)
        adapter = GmailImapAdapter(
            account="gmail_main", source_tag="[GM]", host="imap.gmail.com",
            user="me@gmail.com", password="app-pw", conn_factory=factory,
        )

        self._send(adapter, "900")

        self.assertIn(("EXPUNGE", ("900",)), fake.uid_calls)

    def test_a_refused_list_keeps_the_drafts_copy(self):
        """`imaplib` returns a tagged `NO` quietly, so an unread LIST status reads
        as `no \\Sent mailbox` — the safety gate must still hold and the draft
        survive. (The first LIST resolves Drafts; the refusal is the Sent lookup,
        which runs after delivery and may never fail the send.)"""
        fake = FakeImapConn(fetch_body=self._draft_bytes(),
                            list_statuses=["OK", "NO"])

        _fake_smtp, message_id = self._send(self._yahoo(fake))

        self.assertTrue(message_id, "a delivered message must still report its id")
        self.assertEqual(fake.append_calls, [])
        self.assertEqual([c for c in fake.uid_calls if c[0] == "EXPUNGE"], [],
                         "a refused LIST files no Sent copy, so nothing may be expunged")

    def test_a_refused_list_is_not_cached_as_a_missing_sent_mailbox(self):
        """Caching the empty result of a LIST the server refused makes EVERY later
        send in the process skip Sent and pile up drafts, diagnosed as a Sent
        folder that does not exist. Only a LIST that genuinely answered may be
        cached."""
        fake = FakeImapConn(fetch_body=self._draft_bytes(),
                            list_statuses=["OK", "NO"])
        adapter = self._yahoo(fake)

        self._send(adapter)
        self._send(adapter)

        self.assertEqual(fake.list_calls, 3,
                         "the refused Sent LIST must be retried, not cached "
                         "(and the answered Drafts LIST cached, not repeated)")
        self.assertEqual([c[0] for c in fake.append_calls], ["Sent"],
                         "the recovered LIST must resolve Sent and file the copy")
        self.assertIn(("EXPUNGE", ("901",)), fake.uid_calls)

    def test_a_mailbox_bookkeeping_failure_never_fails_a_delivered_send(self):
        """The message is already in the provider's hands by the time any of this
        runs — reporting a failure would tell the user nothing was sent. (The
        first LIST resolves Drafts, before delivery; the one that blows up is the
        post-delivery Sent lookup.)"""
        fake = FakeImapConn(
            fetch_body=self._draft_bytes(),
            list_errors=[None, imaplib.IMAP4.error("LIST failed")])

        fake_smtp, message_id = self._send(self._yahoo(fake))

        self.assertEqual(len(fake_smtp.sendmail_calls), 1)
        self.assertTrue(message_id, "a delivered message must still report its id")


class ImapDraftsMailboxSpecialUseTest(unittest.TestCase):
    """The Drafts mailbox is resolved by its RFC 6154 `\\Drafts` special-use
    attribute, exactly as Sent is — the literal `"Drafts"` is wrong on Gmail
    (`[Gmail]/Drafts`) and Yahoo (`Draft`), where it makes every APPEND a
    `NO [TRYCREATE]` and every draft SELECT a failure."""

    _GMAIL_LIST = [
        b'(\\HasNoChildren) "/" "INBOX"',
        b'(\\HasNoChildren \\Drafts) "/" "[Gmail]/Drafts"',
        b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"',
    ]

    def _adapter(self, fake):
        factory, _ = _make_conn_factory(fake)
        return GmailImapAdapter(
            account="gmail_main", source_tag="[GM]", host="imap.gmail.com",
            user="me@gmail.com", password="app-pw", conn_factory=factory,
        )

    def _raw(self):
        return _build_raw_message("Draft subject", "me@gmail.com",
                                  "v@example.com", "Body text")

    def test_create_draft_appends_to_the_special_use_drafts_mailbox(self):
        fake = FakeImapConn(list_response=self._GMAIL_LIST)

        self._adapter(fake).create_draft(self._raw())

        self.assertEqual([c[0] for c in fake.append_calls], ["[Gmail]/Drafts"])

    def test_a_listed_provider_name_resolves_when_no_attribute_is_advertised(self):
        """Plenty of servers advertise no special-use at all; the fallback names
        are matched against what the server DID list, never assumed to exist."""
        fake = FakeImapConn(list_response=[b'(\\HasNoChildren) "/" "INBOX"',
                                           b'(\\HasNoChildren) "/" "Draft"'])

        self._adapter(fake).create_draft(self._raw())

        self.assertEqual([c[0] for c in fake.append_calls], ["Draft"])

    def test_an_account_with_no_drafts_mailbox_is_a_structural_error(self):
        fake = FakeImapConn(list_response=[b'(\\HasNoChildren) "/" "INBOX"'])

        with self.assertRaises(RuntimeError) as caught:
            self._adapter(fake).create_draft(self._raw())

        self.assertIn("Drafts", str(caught.exception))
        self.assertEqual(fake.append_calls, [],
                         "nothing may be appended to a mailbox that does not exist")

    def test_a_refused_list_raises_instead_of_assuming_a_drafts_name(self):
        """`imaplib` returns a tagged `NO` quietly — an unread status would append
        the draft to a guessed mailbox name."""
        fake = FakeImapConn(list_statuses=["NO"])

        with self.assertRaises(RuntimeError):
            self._adapter(fake).create_draft(self._raw())

        self.assertEqual(fake.append_calls, [])

    def test_send_draft_reads_the_draft_from_the_resolved_drafts_mailbox(self):
        raw = compose(from_addr="me@gmail.com", to="vendor@example.com",
                      subject="Warranty claim", body="Please assist.")
        fake = FakeImapConn(fetch_body=raw, list_response=self._GMAIL_LIST)
        adapter = self._adapter(fake)

        with patch("vidushi_oa.mail.imap.smtplib.SMTP", return_value=FakeSMTP()):
            adapter.send_draft("900")

        self.assertIn("[Gmail]/Drafts", fake.select_calls)
        self.assertNotIn("Drafts", fake.select_calls)


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
        transport = _JmapSessionOnlyTransport()
        adapter = JmapAdapter(
            account="fastmail_main", source_tag="[FM]", token="secret-token",
            session_url=SESSION_URL, transport=transport,
        )

        self.assertIn("send", adapter.capabilities())


class _JmapSessionOnlyTransport:
    """Answers the session GET and nothing else — for the one test that inspects a
    `JmapAdapter` without issuing a single JMAP call. Every test that does issue
    calls uses `_JmapRoutingTransport`, which routes each one to its own response."""

    def __init__(self, session_response=None):
        self.session_response = session_response or _CANNED_SESSION
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        if method == "GET":
            return 200, self.session_response
        raise AssertionError(f"unexpected {method} to {url} on a session-only transport")


def _returned_or_raised(response):
    """Raise `response` when it is an exception, else hand it back as a payload."""
    if isinstance(response, Exception):
        raise response
    return response


class _JmapRoutingTransport:
    """Routes each call by URL / JMAP method name so the blob upload, the
    `Mailbox/query` and the `Email/import` each get their OWN canned response.

    A flat one-payload-for-every-call fake silently masks a draft carrying neither
    a real blob nor a real mailbox — the exact wiring this fake exists to pin.

    A `Mailbox/query` is additionally routed by its `filter.role`, so the
    `drafts` lookup and the `sent` lookup can answer with different ids: an
    `api` key `"Mailbox/query:<role>"` wins for that role, and the plain
    `"Mailbox/query"` key is the fallback for any role without one.

    An `api` value that is an `Exception` is RAISED instead of returned — the
    live transport surface is wider than a JMAP payload (`urlopen` raises
    `HTTPError`, an `OSError`, on every 4xx/5xx, and `json.loads` raises
    `ValueError` on a non-JSON 2xx body)."""

    def __init__(self, session=None, upload=None, upload_status=200, api=None):
        self.session = _SESSION_WITH_UPLOAD if session is None else session
        self.upload = {"blobId": BLOB_ID} if upload is None else upload
        self.upload_status = upload_status
        self.api = dict(api or {})
        self.api.setdefault("Mailbox/query", _MAILBOX_QUERY_OK)
        self.api.setdefault("Mailbox/query:sent", _SENT_MAILBOX_QUERY_OK)
        self.api.setdefault("Email/import", _IMPORT_OK)
        self.api.setdefault("EmailSubmission/set", _SUBMISSION_OK)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        if method == "GET":
            return 200, self.session
        if url == UPLOAD_URL:
            return self.upload_status, self.upload
        call = body["methodCalls"][0]
        if call[0] == "Mailbox/query":
            role = (call[1].get("filter") or {}).get("role")
            keyed = self.api.get(f"Mailbox/query:{role}")
            if keyed is not None:
                return 200, _returned_or_raised(keyed)
        return 200, _returned_or_raised(
            self.api.get(call[0], {"methodResponses": []}))

    def api_call(self, name):
        """The first `methodCalls` entry posted for JMAP method `name`, or None."""
        calls = self.api_calls(name)
        return calls[0] if calls else None

    def api_calls(self, name):
        """Every `methodCalls` entry posted for JMAP method `name`."""
        return [body["methodCalls"][0] for method, url, _headers, body in self.calls
                if method == "POST" and url != UPLOAD_URL
                and body["methodCalls"][0][0] == name]

    def upload_bodies(self):
        return [body for method, url, _headers, body in self.calls
                if method == "POST" and url == UPLOAD_URL]

    def post_calls(self):
        return [c for c in self.calls if c[0] == "POST"]


class JmapCreateDraftBlobImportTest(unittest.TestCase):
    """§S1 AC (release/1.1.1 code-review): `JmapAdapter.create_draft` uploads the
    composed RFC822 as a blob and imports it into the Drafts mailbox with the
    `$draft` keyword, returning the created email's id. Supersedes the original
    content-less `Email/set` assertion — that path created EMPTY Fastmail drafts."""

    def test_create_draft_imports_the_uploaded_blob_into_drafts_with_draft_keyword(self):
        transport = _JmapRoutingTransport()
        adapter = JmapAdapter(
            account="fastmail_main", source_tag="[FM]", token="secret-token",
            session_url=SESSION_URL, transport=transport,
        )
        raw = _build_raw_message("Draft subject", "me@fastmail.com", "v@example.com", "Body")

        draft_id = adapter.create_draft(raw)

        self.assertEqual(transport.upload_bodies(), [raw],
                         "the literal raw_rfc822 bytes must be uploaded as a blob")
        import_call = transport.api_call("Email/import")
        self.assertIsNotNone(import_call, "create_draft must issue an Email/import")
        imported = list(import_call[1]["emails"].values())
        self.assertEqual(len(imported), 1)
        self.assertEqual(imported[0].get("blobId"), BLOB_ID,
                         "Email/import must reference the uploaded blobId")
        self.assertEqual(imported[0].get("mailboxIds"), {DRAFTS_MAILBOX_ID: True},
                         "Email/import must land in the resolved Drafts mailbox")
        self.assertTrue(imported[0].get("keywords", {}).get("$draft"),
                        f"imported email must carry $draft: {imported[0]!r}")
        self.assertEqual(draft_id, "Md-draft-1")

    def test_blob_upload_accepts_http_201_created(self):
        """Fastmail/Cyrus answers a JMAP blob upload with `201 Created` (RFC 8620
        §6.1 never mandates 200) — treating anything but 200 as a failure makes
        EVERY real draft raise, so any 2xx must be accepted."""
        transport = _JmapRoutingTransport(upload_status=201)
        adapter = JmapAdapter(
            account="fastmail_main", source_tag="[FM]", token="secret-token",
            session_url=SESSION_URL, transport=transport,
        )
        raw = _build_raw_message("Draft subject", "me@fastmail.com", "v@example.com", "Body")

        self.assertEqual(adapter.create_draft(raw), "Md-draft-1")

    def test_session_without_upload_url_raises_instead_of_creating_an_empty_draft(self):
        """`uploadUrl` is a mandatory Session property; without it there is no way
        to transmit the composed content, so a structured failure is the only
        correct outcome — never a silent content-less draft."""
        transport = _JmapRoutingTransport(session=_CANNED_SESSION)
        adapter = JmapAdapter(
            account="fastmail_main", source_tag="[FM]", token="secret-token",
            session_url=SESSION_URL, transport=transport,
        )
        raw = _build_raw_message("Draft subject", "me@fastmail.com", "v@example.com", "Body")

        with self.assertRaises(RuntimeError) as ctx:
            adapter.create_draft(raw)

        self.assertIn("uploadUrl", str(ctx.exception))
        self.assertEqual(transport.post_calls(), [],
                         "no draft-creating POST may be issued without an uploadUrl")

    def test_upload_returning_no_blob_id_raises(self):
        transport = _JmapRoutingTransport(upload={})
        adapter = JmapAdapter(
            account="fastmail_main", source_tag="[FM]", token="secret-token",
            session_url=SESSION_URL, transport=transport,
        )
        raw = _build_raw_message("Draft subject", "me@fastmail.com", "v@example.com", "Body")

        with self.assertRaises(RuntimeError):
            adapter.create_draft(raw)

        self.assertIsNone(transport.api_call("Email/import"),
                          "an import must not be attempted without a blobId")

    def test_no_drafts_mailbox_raises_instead_of_importing_with_empty_mailbox_ids(self):
        """RFC 8621 §4.8 requires at least one mailbox on an `EmailImport`; posting
        `mailboxIds: {}` is a guaranteed server-side rejection reported to the user
        as a successful draft."""
        empty_query = {
            "methodResponses": [
                ["Mailbox/query", {"accountId": ACCOUNT_ID, "ids": []}, "0"],
            ],
        }
        transport = _JmapRoutingTransport(api={"Mailbox/query": empty_query})
        adapter = JmapAdapter(
            account="fastmail_main", source_tag="[FM]", token="secret-token",
            session_url=SESSION_URL, transport=transport,
        )
        raw = _build_raw_message("Draft subject", "me@fastmail.com", "v@example.com", "Body")

        with self.assertRaises(RuntimeError) as ctx:
            adapter.create_draft(raw)

        self.assertIn("Drafts", str(ctx.exception))
        self.assertIsNone(transport.api_call("Email/import"),
                          "no Email/import may be posted with an empty mailboxIds")

    def test_import_rejected_with_not_created_raises_instead_of_empty_draft_id(self):
        """A JMAP method-level rejection arrives inside an HTTP 200 as a
        `notCreated` SetError — returning "" for it reports a phantom draft."""
        not_created = {
            "methodResponses": [
                ["Email/import",
                 {"accountId": ACCOUNT_ID,
                  "notCreated": {"draft": {"type": "invalidProperties",
                                           "properties": ["mailboxIds"]}}}, "0"],
            ],
        }
        transport = _JmapRoutingTransport(api={"Email/import": not_created})
        adapter = JmapAdapter(
            account="fastmail_main", source_tag="[FM]", token="secret-token",
            session_url=SESSION_URL, transport=transport,
        )
        raw = _build_raw_message("Draft subject", "me@fastmail.com", "v@example.com", "Body")

        with self.assertRaises(RuntimeError) as ctx:
            adapter.create_draft(raw)

        self.assertIn("invalidProperties", str(ctx.exception))

    def test_import_method_level_error_response_raises_instead_of_empty_draft_id(self):
        """A failed method call comes back as `["error", {...}, callId]` inside an
        HTTP 200 response — it must not degrade into an empty draft id either."""
        errored = {
            "methodResponses": [
                ["error", {"type": "accountNotFound"}, "0"],
            ],
        }
        transport = _JmapRoutingTransport(api={"Email/import": errored})
        adapter = JmapAdapter(
            account="fastmail_main", source_tag="[FM]", token="secret-token",
            session_url=SESSION_URL, transport=transport,
        )
        raw = _build_raw_message("Draft subject", "me@fastmail.com", "v@example.com", "Body")

        with self.assertRaises(RuntimeError) as ctx:
            adapter.create_draft(raw)

        self.assertIn("accountNotFound", str(ctx.exception))


class JmapImportRejectionIsNeverSilentTest(unittest.TestCase):
    """No `notCreated` SetError is quietly absorbed into a draft id.

    `compose()` stamps a unique `Message-ID`/`Date` per call, so a redraft never
    collides with an earlier content-addressed blob and every import is expected to
    create. A rejection of ANY type — `alreadyExists` included — therefore means no
    draft was created for this call, and must surface rather than resolve to some
    other message's id."""

    def test_an_already_exists_rejection_raises_rather_than_returning_another_id(self):
        already_exists = {
            "methodResponses": [
                ["Email/import",
                 {"accountId": ACCOUNT_ID,
                  "notCreated": {"draft": {"type": "alreadyExists",
                                           "existingId": "Md-draft-existing"}}}, "0"],
            ],
        }
        transport = _JmapRoutingTransport(api={"Email/import": already_exists})
        adapter = JmapAdapter(
            account="fastmail_main", source_tag="[FM]", token="secret-token",
            session_url=SESSION_URL, transport=transport,
        )
        raw = _build_raw_message("Draft subject", "me@fastmail.com", "v@example.com", "Body")

        with self.assertRaises(RuntimeError) as ctx:
            adapter.create_draft(raw)

        self.assertIn("alreadyExists", str(ctx.exception))


class JmapUploadBodyIsAlwaysBytesTest(unittest.TestCase):
    """The upload POST must carry the message as raw bytes. A `str` handed to the
    default `urllib` transport would be JSON-encoded — a quoted, escaped payload
    that uploads cleanly and yields a corrupt draft with no error anywhere."""

    def test_a_str_message_is_uploaded_as_encoded_bytes_not_a_json_string(self):
        transport = _JmapRoutingTransport()
        adapter = JmapAdapter(
            account="fastmail_main", source_tag="[FM]", token="secret-token",
            session_url=SESSION_URL, transport=transport,
        )
        raw = _build_raw_message("Draft subject", "me@fastmail.com", "v@example.com", "Body")

        adapter.create_draft(raw.decode("utf-8"))

        self.assertEqual(transport.upload_bodies(), [raw],
                         "a str message must be encoded to bytes before the upload POST")


class JmapSendDraftFilesTheSentMessageTest(unittest.TestCase):
    """A submitted draft must stop being a draft: `EmailSubmission/set` carries an
    `onSuccessUpdateEmail` patch that clears `$draft` and moves the message into
    the `sent`-role mailbox. Without it every sent message stays in Drafts and
    Sent stays empty — no mailbox record of outbound correspondence."""

    def _adapter(self, transport):
        return JmapAdapter(
            account="fastmail_main", source_tag="[FM]", token="secret-token",
            session_url=SESSION_URL, transport=transport,
        )

    def test_submission_moves_the_email_to_sent_and_clears_the_draft_keyword(self):
        transport = _JmapRoutingTransport()

        self.assertEqual(self._adapter(transport).send_draft("Md-draft-1"), "S-sent-1")

        submission = transport.api_call("EmailSubmission/set")
        self.assertIsNotNone(submission)
        patches = submission[1].get("onSuccessUpdateEmail") or {}
        self.assertEqual(list(patches), ["#submission"],
                         f"the patch must back-reference the created submission; got {patches!r}")
        patch_object = patches["#submission"]
        self.assertEqual(patch_object.get("mailboxIds"), {SENT_MAILBOX_ID: True},
                         "a sent message must be moved into the sent-role mailbox")
        self.assertIn("keywords/$draft", patch_object)
        self.assertIsNone(patch_object["keywords/$draft"],
                          "the $draft keyword must be cleared on a successful send")

    def test_an_account_with_no_sent_mailbox_still_clears_the_draft_keyword(self):
        """A missing Sent mailbox is no reason to refuse to send — unlike an import,
        a submission needs no mailbox — but the message must stop being a draft."""
        empty = {
            "methodResponses": [
                ["Mailbox/query", {"accountId": ACCOUNT_ID, "ids": []}, "0"],
            ],
        }
        transport = _JmapRoutingTransport(api={"Mailbox/query:sent": empty})

        self.assertEqual(self._adapter(transport).send_draft("Md-draft-1"), "S-sent-1")

        patch_object = transport.api_call("EmailSubmission/set")[1]["onSuccessUpdateEmail"]["#submission"]
        self.assertNotIn("mailboxIds", patch_object)
        self.assertIsNone(patch_object["keywords/$draft"])

    def test_a_failed_sent_mailbox_query_does_not_block_the_submission(self):
        """The Sent lookup is a convenience, not a precondition. A `Mailbox/query`
        that fails at the method level must cost the user the move to Sent — never
        the send itself, which needs no mailbox at all."""
        errored = {
            "methodResponses": [
                ["error", {"type": "accountNotFound"}, "0"],
            ],
        }
        transport = _JmapRoutingTransport(api={"Mailbox/query:sent": errored})

        self.assertEqual(self._adapter(transport).send_draft("Md-draft-1"), "S-sent-1")

        patch_object = transport.api_call("EmailSubmission/set")[1]["onSuccessUpdateEmail"]["#submission"]
        self.assertNotIn("mailboxIds", patch_object)
        self.assertIsNone(patch_object["keywords/$draft"])

    def test_a_transport_level_sent_lookup_failure_does_not_block_the_submission(self):
        """The live failure surface of the Sent lookup is wider than a JMAP payload:
        `urlopen` raises `HTTPError` (an `OSError`) on every 4xx/5xx — so the
        status-check `RuntimeError` never even fires against a real server — and a
        2xx carrying a captive-portal HTML body raises `ValueError` from
        `json.loads`. Neither may cost the user the send."""
        for failure in (OSError("HTTP Error 503: Service Unavailable"),
                        ValueError("Expecting value: line 1 column 1 (char 0)")):
            with self.subTest(failure=type(failure).__name__):
                transport = _JmapRoutingTransport(api={"Mailbox/query:sent": failure})

                self.assertEqual(
                    self._adapter(transport).send_draft("Md-draft-1"), "S-sent-1")

                patch_object = transport.api_call(
                    "EmailSubmission/set")[1]["onSuccessUpdateEmail"]["#submission"]
                self.assertNotIn("mailboxIds", patch_object)
                self.assertIsNone(patch_object["keywords/$draft"])

    def test_a_drafts_lookup_failure_still_fails_the_draft_import(self):
        """Only the SENT lookup is best-effort. An import genuinely needs a mailbox,
        so a Drafts lookup that fails at the transport level must still surface."""
        transport = _JmapRoutingTransport(
            api={"Mailbox/query:drafts": OSError("HTTP Error 503")})
        raw = _build_raw_message("Draft subject", "me@fastmail.com", "v@example.com", "Body")

        with self.assertRaises(OSError):
            self._adapter(transport).create_draft(raw)

        self.assertIsNone(transport.api_call("Email/import"))


class JmapDraftsMailboxQueryFailureTest(unittest.TestCase):
    """A `Mailbox/query` that fails at the METHOD level still arrives inside an
    HTTP 200, so a status-only check mis-reports an auth/account failure as "your
    account has no Drafts mailbox" — an actively misleading diagnosis. The real
    server error must surface, and a failed query must not be cached as ""."""

    def test_mailbox_query_method_level_error_surfaces_the_server_error(self):
        errored = {
            "methodResponses": [
                ["error", {"type": "accountNotFound"}, "0"],
            ],
        }
        transport = _JmapRoutingTransport(api={"Mailbox/query": errored})
        adapter = JmapAdapter(
            account="fastmail_main", source_tag="[FM]", token="secret-token",
            session_url=SESSION_URL, transport=transport,
        )
        raw = _build_raw_message("Draft subject", "me@fastmail.com", "v@example.com", "Body")

        with self.assertRaises(RuntimeError) as ctx:
            adapter.create_draft(raw)

        self.assertIn("accountNotFound", str(ctx.exception))
        self.assertNotIn("no Drafts mailbox", str(ctx.exception))
        self.assertIsNone(transport.api_call("Email/import"),
                          "no import may be posted when the mailbox query failed")

    def test_missing_mailbox_query_response_surfaces_a_structured_error(self):
        transport = _JmapRoutingTransport(api={"Mailbox/query": {"methodResponses": []}})
        adapter = JmapAdapter(
            account="fastmail_main", source_tag="[FM]", token="secret-token",
            session_url=SESSION_URL, transport=transport,
        )
        raw = _build_raw_message("Draft subject", "me@fastmail.com", "v@example.com", "Body")

        with self.assertRaises(RuntimeError) as ctx:
            adapter.create_draft(raw)

        self.assertIn("Mailbox/query", str(ctx.exception))
        self.assertNotIn("no Drafts mailbox", str(ctx.exception))

    def test_a_failed_mailbox_query_is_not_cached_and_is_retried(self):
        """Caching the empty result makes every later `create_draft` in the process
        repeat the wrong diagnosis without ever re-querying the server."""
        errored = {
            "methodResponses": [
                ["error", {"type": "serverUnavailable"}, "0"],
            ],
        }
        transport = _JmapRoutingTransport(api={"Mailbox/query": errored})
        adapter = JmapAdapter(
            account="fastmail_main", source_tag="[FM]", token="secret-token",
            session_url=SESSION_URL, transport=transport,
        )
        raw = _build_raw_message("Draft subject", "me@fastmail.com", "v@example.com", "Body")

        with self.assertRaises(RuntimeError):
            adapter.create_draft(raw)

        transport.api["Mailbox/query"] = _MAILBOX_QUERY_OK

        self.assertEqual(adapter.create_draft(raw), "Md-draft-1")
        import_call = transport.api_call("Email/import")
        self.assertIsNotNone(import_call)
        imported = list(import_call[1]["emails"].values())[0]
        self.assertEqual(imported.get("mailboxIds"), {DRAFTS_MAILBOX_ID: True})


class JmapSendDraftFailureTest(unittest.TestCase):
    """The same silent-empty-id class of bug on the send path: an
    `EmailSubmission/set` rejected with `notCreated` must raise, never return ""
    and be reported to the user as a sent message."""

    def test_submission_rejected_with_not_created_raises(self):
        not_created = {
            "methodResponses": [
                ["EmailSubmission/set",
                 {"accountId": ACCOUNT_ID,
                  "notCreated": {"submission": {"type": "forbiddenFrom"}}}, "0"],
            ],
        }
        transport = _JmapRoutingTransport(api={"EmailSubmission/set": not_created})
        adapter = JmapAdapter(
            account="fastmail_main", source_tag="[FM]", token="secret-token",
            session_url=SESSION_URL, transport=transport,
        )

        with self.assertRaises(RuntimeError) as ctx:
            adapter.send_draft("Md-draft-1")

        self.assertIn("forbiddenFrom", str(ctx.exception))


class JmapSendDraftEmailSubmissionTest(unittest.TestCase):
    """§S1 AC: `JmapAdapter.send_draft` issues exactly ONE
    `EmailSubmission/set` referencing the draft's email id."""

    def test_send_draft_issues_exactly_one_email_submission_set_referencing_the_draft(self):
        transport = _JmapRoutingTransport()
        adapter = JmapAdapter(
            account="fastmail_main", source_tag="[FM]", token="secret-token",
            session_url=SESSION_URL, transport=transport,
        )

        message_id = adapter.send_draft("Md-draft-1")

        submission_calls = transport.api_calls("EmailSubmission/set")
        self.assertEqual(len(submission_calls), 1,
                         "exactly one EmailSubmission/set may be issued per send")
        created_objects = list(submission_calls[0][1]["create"].values())
        self.assertEqual(len(created_objects), 1)
        self.assertEqual(created_objects[0].get("emailId"), "Md-draft-1")
        self.assertTrue(message_id)


class JmapCreateDraftTransmitsComposedContentTest(unittest.TestCase):
    """Release/1.1.1 code-review bugfix — `create_draft` DISCARDS its
    `raw_rfc822` argument today: it issues only a content-less `Email/set`
    (`{"keywords": {"$draft": True}}`), so the composed message is never
    actually transmitted and a Fastmail draft comes out EMPTY.

    Pinned shape for GREEN — the correct JMAP "import raw RFC822" flow:
      1. `_session()` must ALSO cache `uploadUrl` from the session document
         (it does not today — only `apiUrl`/`accountId` are cached).
      2. `create_draft` must POST the literal `raw_rfc822` bytes (NOT
         JSON-wrapped) to that `uploadUrl`, receiving back a JMAP
         `{"blobId": ..., ...}` upload response.
      3. That `blobId` must then be referenced by an `Email/import` (or an
         `Email/set` `create` whose object carries the blob, e.g. via a
         `bodyStructure`/`blobId` field) landing in the Drafts mailbox with
         the `$draft` keyword still set.

    Beyond the composed bytes reaching the wire, the draft is only real when
    the `Email/import` actually REFERENCES the uploaded blob and the resolved
    Drafts mailbox — an import carrying an empty `blobId`/`mailboxIds` is a
    server-side rejection dressed up as a successful draft, so both are pinned
    here against a transport that answers each call distinctly.
    """

    def test_create_draft_transmits_the_composed_message_content(self):
        raw = compose(
            from_addr="me@fastmail.com",
            to="v@example.com",
            subject="Warranty claim for X200",
            body="Please advise on RMA for my X200 unit.",
        )
        transport = _JmapRoutingTransport()
        adapter = JmapAdapter(
            account="fastmail_main", source_tag="[FM]", token="secret-token",
            session_url=SESSION_URL, transport=transport,
        )

        adapter.create_draft(raw)

        # Inspect EVERY call the adapter made through its transport (GET
        # session + any POSTs) for the composed content — a blob upload would
        # carry it as the literal `raw_rfc822` bytes as the call's `body`; an
        # inline `Email/set`/`Email/import` would carry it inside a POST
        # body dict (e.g. a `bodyStructure`/textBody value).
        transmitted_bodies = [call[3] for call in transport.calls]
        transmitted_repr = repr(transmitted_bodies)

        self.assertIn(
            "Warranty claim for X200", transmitted_repr,
            "create_draft must transmit the composed Subject — it must not "
            "discard raw_rfc822 and send only {'keywords': {'$draft': True}}",
        )
        self.assertIn(
            "Please advise on RMA for my X200 unit.", transmitted_repr,
            "create_draft must transmit the composed body text",
        )
        self.assertIn(
            "v@example.com", transmitted_repr,
            "create_draft must transmit the composed To address",
        )
        self.assertIn(
            raw, transmitted_bodies,
            "create_draft must upload the literal raw_rfc822 bytes as a blob "
            "(a POST whose body IS raw, not JSON-wrapped) to the session's "
            "uploadUrl — today no call carries the raw bytes at all",
        )

        import_call = transport.api_call("Email/import")
        self.assertIsNotNone(
            import_call,
            "the uploaded blob must be imported — a blob nobody imports is not a draft")
        imported = list(import_call[1]["emails"].values())[0]
        self.assertEqual(
            imported.get("blobId"), BLOB_ID,
            "Email/import must reference the blobId the upload returned, not \"\"")
        self.assertEqual(
            imported.get("mailboxIds"), {DRAFTS_MAILBOX_ID: True},
            "Email/import must reference the Mailbox/query-resolved Drafts id, not {}")


if __name__ == "__main__":
    unittest.main()
