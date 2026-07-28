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

from vidushi_oa.mail.compose import compose
from vidushi_oa.mail.imap import GmailImapAdapter, YahooImapAdapter
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

UPLOAD_URL_TEMPLATE = "https://api.fastmail.com/upload/{accountId}/"
UPLOAD_URL = f"https://api.fastmail.com/upload/{ACCOUNT_ID}/"
DRAFTS_MAILBOX_ID = "Mb-drafts"
BLOB_ID = "Gb-blob-1"

# Every real Fastmail session advertises `uploadUrl` (RFC 8620 §2 makes it a
# mandatory Session property) — the blob+import draft flow is the only path.
_SESSION_WITH_UPLOAD = dict(_CANNED_SESSION, uploadUrl=UPLOAD_URL_TEMPLATE)

_MAILBOX_QUERY_OK = {
    "methodResponses": [
        ["Mailbox/query", {"accountId": ACCOUNT_ID, "ids": [DRAFTS_MAILBOX_ID]}, "0"],
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
    tests need: `login`, `select`, `append`, and a UID `FETCH` (recording every
    call), plus a canned `append` response shaped like real
    `imaplib.IMAP4.append()`. `fetch_body` is the raw draft bytes returned by a
    `conn.uid("FETCH", uid, "(BODY[])")` — shaped like imaplib's
    `(descriptor, raw_bytes)` tuple item — so `send_draft` reads back a real
    stored draft rather than fabricating one."""

    def __init__(self, append_response=None, fetch_body=None):
        self.append_response = append_response or ("OK", [b"[APPENDUID 1 900] (Success)"])
        self.fetch_body = fetch_body
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
        return self.append_response

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


class _JmapRoutingTransport:
    """Routes each call by URL / JMAP method name so the blob upload, the
    `Mailbox/query` and the `Email/import` each get their OWN canned response.

    `_JmapFakeTransport`'s flat queue hands the same payload to all three, which
    silently masks a draft carrying neither a real blob nor a real mailbox — the
    exact wiring this fake exists to pin."""

    def __init__(self, session=None, upload=None, upload_status=200, api=None):
        self.session = _SESSION_WITH_UPLOAD if session is None else session
        self.upload = {"blobId": BLOB_ID} if upload is None else upload
        self.upload_status = upload_status
        self.api = dict(api or {})
        self.api.setdefault("Mailbox/query", _MAILBOX_QUERY_OK)
        self.api.setdefault("Email/import", _IMPORT_OK)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        if method == "GET":
            return 200, self.session
        if url == UPLOAD_URL:
            return self.upload_status, self.upload
        return 200, self.api.get(body["methodCalls"][0][0], {"methodResponses": []})

    def api_call(self, name):
        """The single `methodCalls` entry posted for JMAP method `name`, or None."""
        for method, url, _headers, body in self.calls:
            if method == "POST" and url != UPLOAD_URL \
                    and body["methodCalls"][0][0] == name:
                return body["methodCalls"][0]
        return None

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
        transport = _JmapFakeTransport(responses=[not_created])
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
