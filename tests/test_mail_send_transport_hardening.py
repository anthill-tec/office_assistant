"""Mail send/draft transport hardening (release/1.1.1 code-review) — RED.

Three real bugs the E2E emulator tier surfaced in the send/draft transport,
none of which are fixed yet:

  FIX 1 — SECURITY: `vidushi_oa/mail/imap.py` opens `imaplib.IMAP4_SSL(h, p)`
    (in `ImapAdapter.__init__`'s default `conn_factory`) and calls
    `smtp.starttls()` (in `ImapAdapter.send_draft`) with NO `ssl`/`context`
    argument at all. Python's `imaplib.IMAP4_SSL` then builds its context via
    `ssl._create_stdlib_context()` — CERT_NONE, `check_hostname=False` — so
    neither the IMAP nor the SMTP submission channel verifies the server's
    certificate or hostname against real Gmail/Yahoo/Fastmail hosts. Every
    assertion below fails today because NO context is ever passed (not because
    the wrong one is): `kwargs.get("ssl_context")` / `kwargs.get("context")` is
    `None`, so `assertIsInstance(..., ssl.SSLContext)` fails outright. There is
    also no `tls_verify` field on the endpoint override
    (`tests/test_mail_endpoint_override.py`'s shape) for the emulator's
    self-signed cert to opt out with.

  FIX 2 — CORRECTNESS: `JmapAdapter.send_draft` (vidushi_oa/mail/jmap.py)
    issues `EmailSubmission/set` with only `{"emailId": draft_id}` in the
    `create` object. RFC 8621 §7.1 requires `identityId` too. The adapter never
    issues an `Identity/get` at all today, so the tests below fail because no
    such call is ever made and no `identityId` key is ever present.

  FIX 3 — the Sent/Drafts IMAP `APPEND` (`ImapAdapter.create_draft` and
    `ImapAdapter._file_sent_copy`) hands the resolved mailbox name to
    `conn.append(mailbox, ...)` completely unquoted. Real `imaplib.IMAP4.append`
    forwards `mailbox` to the wire VERBATIM (see its `_simple_command` call) —
    it does no quoting of its own — so a mailbox whose name contains a space
    (e.g. Yahoo's `"Sent Items"`) breaks the command. The fixture LIST
    responses below advertise such a name; the tests fail today because the
    recorded `append()` call carries the bare unquoted name, not the RFC 3501
    double-quoted string.

No production code is touched here — tests only. No real network anywhere:
IMAP is faked via an injected `conn_factory` (or a patched
`vidushi_oa.mail.imap.imaplib.IMAP4_SSL`); SMTP is faked via a patched
`vidushi_oa.mail.imap.smtplib.SMTP`; JMAP is faked via an injected transport
callable — the same conventions `tests/test_cr_oa_022_send_transport.py` and
`tests/test_mail_endpoint_override.py` already use.
"""
import ssl
import unittest
from unittest.mock import MagicMock, patch

from vidushi_oa.mail.compose import compose
from vidushi_oa.mail.factory import _default_adapter_factory
from vidushi_oa.mail.imap import GmailImapAdapter, YahooImapAdapter
from vidushi_oa.mail.jmap import JmapAdapter

SESSION_URL = "https://api.fastmail.com/jmap/session"
API_URL = "https://api.fastmail.com/jmap/api/"
ACCOUNT_ID = "u1234567"
_MAIL_CAPABILITY = "urn:ietf:params:jmap:mail"

_SESSION = {
    "apiUrl": API_URL,
    "primaryAccounts": {_MAIL_CAPABILITY: ACCOUNT_ID},
}

_SUBMISSION_OK = {
    "methodResponses": [
        ["EmailSubmission/set",
         {"accountId": ACCOUNT_ID, "created": {"submission": {"id": "S-sent-1"}}}, "0"],
    ],
}


class _FakeResolver:
    """Deterministic `SecretResolver` stand-in — no keyring/file I/O (same shape
    as `tests/test_mail_endpoint_override.py`'s `FakeResolver`)."""

    def resolve(self, ref):
        return f"secret-for-{ref}"


class FakeImapConn:
    """Minimal fake IMAP connection for the FIX1(c)/FIX3 tests below: records
    every `append()` call VERBATIM — the mailbox argument exactly as the
    adapter passed it, with no quoting applied here (matching real
    `imaplib.IMAP4.append`, which forwards the mailbox argument to the wire
    unmodified) — and answers a LIST with whatever special-use response the
    test configures."""

    _DEFAULT_LIST = [
        b'(\\HasNoChildren \\Drafts) "/" "Drafts"',
        b'(\\HasNoChildren \\Sent) "/" "Sent"',
    ]

    def __init__(self, fetch_body=None, list_response=None):
        self.fetch_body = fetch_body
        self.list_response = list(
            self._DEFAULT_LIST if list_response is None else list_response)
        self.login_calls = []
        self.select_calls = []
        self.append_calls = []
        self.uid_calls = []

    def login(self, user, password):
        self.login_calls.append((user, password))
        return ("OK", [b"Logged in"])

    def select(self, mailbox="INBOX", readonly=False):
        self.select_calls.append(mailbox)
        return ("OK", [b"1"])

    def append(self, mailbox, flags, date_time, message):
        self.append_calls.append((mailbox, flags, date_time, message))
        return ("OK", [b"[APPENDUID 1 900] (Success)"])

    def list(self, directory='""', pattern="*"):
        return ("OK", list(self.list_response))

    def uid(self, command, *args):
        self.uid_calls.append((command, args))
        if command == "FETCH":
            uid = args[0]
            body = self.fetch_body or b""
            return ("OK", [(f"{uid} (BODY[] {{{len(body)}}}".encode(), body), b")"])
        if command == "STORE":
            return ("OK", [b"Completed"])
        return ("OK", [b""])


def _conn_factory(fake):
    def factory(host, port):
        return fake
    return factory


# ---------------------------------------------------------------------------
# FIX 1(a)/(c) — IMAP TLS verification
# ---------------------------------------------------------------------------

class ImapTlsVerificationTest(unittest.TestCase):
    """FIX1(a)+(c): a real account's IMAP connection must verify the server
    certificate/hostname by default; an explicit `tls_verify: false` endpoint
    override (the emulator's opt-out) must yield a non-verifying context."""

    def test_a_real_account_imap_connection_uses_a_verifying_ssl_context(self):
        adapter = GmailImapAdapter(
            account="gmail_main", source_tag="[GM]", host="imap.gmail.com",
            user="me@gmail.com", password="app-pw",
        )  # no conn_factory override -> exercises the real default factory

        with patch("vidushi_oa.mail.imap.imaplib.IMAP4_SSL") as imap4_ssl_cls:
            adapter._conn()

        _args, kwargs = imap4_ssl_cls.call_args
        context = kwargs.get("ssl_context")
        self.assertIsInstance(
            context, ssl.SSLContext,
            f"IMAP4_SSL must be given a verifying ssl_context for a real "
            f"account; got call kwargs={kwargs!r}")
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_tls_verify_false_endpoint_override_yields_a_non_verifying_imap_context(self):
        with patch("vidushi_oa.mail.imap.imaplib.IMAP4_SSL") as imap4_ssl_cls:
            adapter = _default_adapter_factory(
                provider="gmail", account="g_emu", address="me@emu.test",
                secret_ref="r1", resolver=_FakeResolver(),
                endpoint={"imap_host": "emu.local", "imap_port": 1143,
                         "tls_verify": False},
            )
            adapter._conn()

        _args, kwargs = imap4_ssl_cls.call_args
        context = kwargs.get("ssl_context")
        self.assertIsInstance(
            context, ssl.SSLContext,
            f"an explicit tls_verify: false override must still hand IMAP4_SSL "
            f"a real (non-verifying) ssl_context, not omit it; "
            f"got call kwargs={kwargs!r}")
        self.assertEqual(context.verify_mode, ssl.CERT_NONE)
        self.assertFalse(context.check_hostname)


# ---------------------------------------------------------------------------
# FIX 1(b)/(c) — SMTP STARTTLS verification
# ---------------------------------------------------------------------------

class SmtpTlsVerificationTest(unittest.TestCase):
    """FIX1(b)+(c): `send_draft`'s STARTTLS must verify by default; the same
    `tls_verify: false` opt-out must reach the SMTP side too."""

    def _draft_bytes(self, from_addr):
        return compose(from_addr=from_addr, to="support@example.com",
                       subject="Return request", body="Requesting an RMA.")

    def test_send_draft_starttls_uses_a_verifying_ssl_context_for_a_real_account(self):
        fake_imap = FakeImapConn(fetch_body=self._draft_bytes("me@gmail.com"))
        adapter = GmailImapAdapter(
            account="gmail_main", source_tag="[GM]", host="imap.gmail.com",
            user="me@gmail.com", password="app-pw",
            conn_factory=_conn_factory(fake_imap),
        )
        fake_smtp = MagicMock()
        fake_smtp.sendmail.return_value = {}

        with patch("vidushi_oa.mail.imap.smtplib.SMTP", return_value=fake_smtp):
            adapter.send_draft("900")

        fake_smtp.starttls.assert_called_once()
        _args, kwargs = fake_smtp.starttls.call_args
        context = kwargs.get("context")
        self.assertIsInstance(
            context, ssl.SSLContext,
            f"send_draft's STARTTLS must pass a verifying ssl context for a "
            f"real account; got starttls call kwargs={kwargs!r}")
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_tls_verify_false_endpoint_override_yields_a_non_verifying_smtp_context(self):
        fake_imap = FakeImapConn(fetch_body=self._draft_bytes("me@emu.test"))
        adapter = _default_adapter_factory(
            provider="yahoo", account="y_emu", address="me@emu.test",
            secret_ref="r1", resolver=_FakeResolver(),
            endpoint={"smtp_host": "emu.local", "smtp_port": 1587,
                     "tls_verify": False},
            conn_factory=_conn_factory(fake_imap),
        )
        fake_smtp = MagicMock()
        fake_smtp.sendmail.return_value = {}

        with patch("vidushi_oa.mail.imap.smtplib.SMTP", return_value=fake_smtp):
            adapter.send_draft("901")

        _args, kwargs = fake_smtp.starttls.call_args
        context = kwargs.get("context")
        self.assertIsInstance(
            context, ssl.SSLContext,
            f"an explicit tls_verify: false override must still hand STARTTLS "
            f"a real (non-verifying) ssl context, not omit it; "
            f"got starttls call kwargs={kwargs!r}")
        self.assertEqual(context.verify_mode, ssl.CERT_NONE)
        self.assertFalse(context.check_hostname)


# ---------------------------------------------------------------------------
# FIX 2 — JMAP EmailSubmission/set must carry a resolved identityId
# ---------------------------------------------------------------------------

class _JmapIdentityTransport:
    """Routes each JMAP call by method name; records every call so the test can
    prove an `Identity/get` was actually issued and inspect the exact
    `EmailSubmission/set` create object posted."""

    def __init__(self, identity_get):
        self.identity_get = identity_get
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        if method == "GET":
            return 200, _SESSION
        call = body["methodCalls"][0]
        if call[0] == "Identity/get":
            return 200, self.identity_get
        if call[0] == "EmailSubmission/set":
            return 200, _SUBMISSION_OK
        return 200, {"methodResponses": []}

    def api_calls(self, name):
        return [body["methodCalls"][0] for method, url, _headers, body in self.calls
                if method == "POST" and body["methodCalls"][0][0] == name]


class JmapSendDraftIdentityTest(unittest.TestCase):
    """FIX2 (RFC 8621 §7.1): `EmailSubmission/set`'s `create` object must carry
    a non-empty `identityId` resolved for the account, not just `emailId`."""

    def _transport(self, identity_id="I-identity-1", email="me@fastmail.com"):
        identity_get = {
            "methodResponses": [
                ["Identity/get", {"accountId": ACCOUNT_ID,
                                  "list": [{"id": identity_id, "email": email}]},
                 "0"],
            ],
        }
        return _JmapIdentityTransport(identity_get)

    def test_send_draft_resolves_the_account_identity_via_identity_get(self):
        transport = self._transport()
        adapter = JmapAdapter(
            account="fastmail_main", source_tag="[FM]", token="secret-token",
            session_url=SESSION_URL, transport=transport,
        )

        adapter.send_draft("Md-draft-1")

        identity_calls = transport.api_calls("Identity/get")
        self.assertTrue(
            identity_calls,
            "send_draft must resolve the account's identity via an "
            "Identity/get call before submitting")

    def test_send_draft_includes_the_resolved_identity_id_in_the_submission_create(self):
        transport = self._transport(identity_id="I-identity-42")
        adapter = JmapAdapter(
            account="fastmail_main", source_tag="[FM]", token="secret-token",
            session_url=SESSION_URL, transport=transport,
        )

        adapter.send_draft("Md-draft-1")

        submission_calls = transport.api_calls("EmailSubmission/set")
        self.assertEqual(len(submission_calls), 1)
        create = submission_calls[0][1]["create"]["submission"]
        self.assertEqual(
            create.get("identityId"), "I-identity-42",
            f"EmailSubmission/set create must carry the resolved identityId "
            f"(RFC 8621 sec7.1); got {create!r}")
        self.assertEqual(create.get("emailId"), "Md-draft-1",
                         "the emailId must still reference the submitted draft")


# ---------------------------------------------------------------------------
# FIX 3 — IMAP APPEND must quote a mailbox name containing a space
# ---------------------------------------------------------------------------

class ImapAppendQuotesMailboxNamesWithSpacesTest(unittest.TestCase):
    """FIX3 (RFC 3501): a resolved Sent/Drafts mailbox name containing a space
    must be double-quoted before being handed to `conn.append()` — real
    `imaplib.IMAP4.append` forwards the mailbox argument to the wire verbatim,
    doing no quoting of its own."""

    def _draft_bytes(self):
        return compose(from_addr="me@yahoo.com", to="support@example.com",
                       subject="Return request", body="Requesting an RMA.")

    def test_sent_mailbox_append_quotes_a_name_containing_a_space(self):
        fake = FakeImapConn(
            fetch_body=self._draft_bytes(),
            list_response=[b'(\\HasNoChildren) "/" "INBOX"',
                           b'(\\HasNoChildren \\Drafts) "/" "Drafts"',
                           b'(\\HasNoChildren \\Sent) "/" "Sent Items"'])
        adapter = YahooImapAdapter(
            account="yahoo_main", source_tag="[YH]", host="imap.mail.yahoo.com",
            user="me@yahoo.com", password="app-pw", conn_factory=_conn_factory(fake),
        )
        fake_smtp = MagicMock()
        fake_smtp.sendmail.return_value = {}

        with patch("vidushi_oa.mail.imap.smtplib.SMTP", return_value=fake_smtp):
            adapter.send_draft("901")

        sent_appends = [c for c in fake.append_calls if "Sent" in c[0]]
        self.assertEqual(
            len(sent_appends), 1,
            f"exactly one APPEND to the Sent mailbox expected; got "
            f"{fake.append_calls!r}")
        mailbox_arg = sent_appends[0][0]
        self.assertEqual(
            mailbox_arg, '"Sent Items"',
            f"a mailbox name containing a space must be double-quoted per "
            f"RFC 3501 before being passed to conn.append(); got {mailbox_arg!r}")

    def test_create_draft_append_quotes_a_drafts_mailbox_name_containing_a_space(self):
        fake = FakeImapConn(
            list_response=[b'(\\HasNoChildren) "/" "INBOX"',
                           b'(\\HasNoChildren \\Drafts) "/" "Draft Items"',
                           b'(\\HasNoChildren \\Sent) "/" "Sent"'])
        adapter = YahooImapAdapter(
            account="yahoo_main", source_tag="[YH]", host="imap.mail.yahoo.com",
            user="me@yahoo.com", password="app-pw", conn_factory=_conn_factory(fake),
        )
        raw = compose(from_addr="me@yahoo.com", to="v@example.com",
                      subject="Draft subject", body="Body text")

        adapter.create_draft(raw)

        self.assertEqual(len(fake.append_calls), 1)
        mailbox_arg = fake.append_calls[0][0]
        self.assertEqual(
            mailbox_arg, '"Draft Items"',
            f"a Drafts mailbox name containing a space must be double-quoted "
            f"per RFC 3501 before being passed to conn.append(); got "
            f"{mailbox_arg!r}")


if __name__ == "__main__":
    unittest.main()
