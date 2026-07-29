"""CR-OA-032 §S1 — `mail-get` returns a decoded message body (RED).

`voa mail-get --account <a> --uid <uid>` returns the ENVELOPE ONLY today:
`_mail_row()` (`vidushi_oa/_cli.py:799-803`) projects a fixed
`id/uid/account/source_tag/subject/sender/date` tuple with no `body`/`attachments`
key at all, `Message` (`vidushi_oa/mail/base.py`) has no `body`/`attachments` field
to carry either, and neither adapter's `fetch_message` requests body content:
`ImapAdapter.fetch_message` -> `_fetch()` -> `_fetch_spec()` requests only
`BODY.PEEK[HEADER.FIELDS (...)]` (`vidushi_oa/mail/imap.py:458-460`), and
`JmapAdapter.fetch_message` (`vidushi_oa/mail/jmap.py:603-624`) requests only
`_EMAIL_PROPERTIES` (header fields) via `Email/get` — no `textBody`/`htmlBody`/
`bodyValues`/`attachments`. So every behavioural test below fails today with a
missing/`None` `body`/`attachments` key, not a crash — proving the gap the CR
describes: opening a shipping mail to read an order number/AWB is still
impossible through `voa`.

Pinned shapes for GREEN (this file is the RED-authored contract):
  - `cmd_mail_get`'s `--json` payload carries a `"body"` key: the decoded
    `text/plain` part when present, else the `text/html` part stripped to text
    (no tags). Transfer-encodings (base64, quoted-printable) are decoded and the
    declared charset honoured.
  - The same payload carries an `"attachments"` key: a list of
    `{"filename": ..., "size": ...}` dicts (the DECODED byte size) — never the
    attachment's bytes, base64 blob, or (for JMAP) its `blobId` content.
  - A JMAP method-level `Email/get` error (`["error", {"type": ..., "description":
    ...}, callId]`) must make `JmapAdapter.fetch_message` raise `RuntimeError`
    naming that error's `type`/`description` (routing through the shared
    `_raise_for_method_error` CR-OA-030 introduced) — `cmd_mail_get` then reports
    that same detail via its existing `_MAIL_LIVE_ERRORS` seam, not
    "message not found".
  - `cmd_mail_get`'s docstring no longer claims `JmapAdapter` raises
    `NotImplementedError` (superseded by CR-OA-028's real `fetch_message`).

Both adapters are covered (§S1 AC "Both adapters satisfy the above"): IMAP via a
fake `imaplib`-shaped connection (the CR-OA-028 `FakeImapConn` UID-FETCH-tuple
pattern, extended with real multipart/encoded message bytes built with
`email.mime`/hand-rolled raw bytes), JMAP via a fake transport (the CR-OA-020/028
`FakeTransport` GET-session / POST-methodCalls pattern). No real network anywhere.
"""
import base64
import contextlib
import inspect
import io
import json
import quopri
import unittest
from argparse import Namespace
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import vidushi_oa._cli as cli
from vidushi_oa.mail.client import MailClient
from vidushi_oa.mail.imap import GmailImapAdapter
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


def _invoke_cmd_mail_get(client, account, uid):
    """Run `cli.cmd_mail_get` against `client` with `_FMT='json'`, capturing
    whatever it prints. Returns `(payload_or_None, exit_code_or_None)` —
    `exit_code` is set only when the handler called `sys.exit`, mirroring the
    seam `tests/test_cr_oa_026_mail_row_uid.py` already drives `cmd_mail_get`
    through (bare `Namespace(account=..., uid=...)`, `cli._FMT`, captured stdout)."""
    original_build_client = cli.build_client
    original_fmt = getattr(cli, "_FMT", "toon")
    cli.build_client = lambda **kw: client
    cli._FMT = "json"
    buf = io.StringIO()
    exit_code = None
    try:
        with contextlib.redirect_stdout(buf):
            try:
                cli.cmd_mail_get(Namespace(account=account, uid=uid))
            except SystemExit as e:
                exit_code = e.code
    finally:
        cli.build_client = original_build_client
        cli._FMT = original_fmt
    text = buf.getvalue().strip()
    payload = json.loads(text) if text else None
    return payload, exit_code


# ---------------------------------------------------------------------------
# IMAP fixtures
# ---------------------------------------------------------------------------

def _build_plain_message(subject, from_addr, to_addr, body_text):
    msg = MIMEText(body_text, "plain")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Message-ID"] = "<plain-body@example.com>"
    return msg.as_bytes()


def _build_html_only_message(subject, from_addr, to_addr, html_text):
    msg = MIMEText(html_text, "html")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Message-ID"] = "<html-only-body@example.com>"
    return msg.as_bytes()


def _build_encoded_plain_message(subject, from_addr, to_addr, body_text, encoding):
    """A real raw RFC 5322 message whose `text/plain` part is genuinely
    transfer-encoded (base64 or quoted-printable) off the wire — hand-built
    (not pre-decoded then re-labelled) so decoding it is a real assertion."""
    if encoding == "base64":
        cte = "base64"
        encoded_body = base64.encodebytes(body_text.encode("utf-8")).decode("ascii")
    else:
        cte = "quoted-printable"
        encoded_body = quopri.encodestring(body_text.encode("utf-8")).decode("ascii")
    raw = (
        f"Subject: {subject}\r\n"
        f"From: {from_addr}\r\n"
        f"To: {to_addr}\r\n"
        f"Message-ID: <{encoding}-body@example.com>\r\n"
        "MIME-Version: 1.0\r\n"
        'Content-Type: text/plain; charset="utf-8"\r\n'
        f"Content-Transfer-Encoding: {cte}\r\n"
        "\r\n"
        f"{encoded_body}\r\n"
    )
    return raw.encode("ascii")


def _build_message_with_attachment(subject, from_addr, to_addr, body_text,
                                    filename, attachment_bytes):
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Message-ID"] = "<attachment-body@example.com>"
    msg.attach(MIMEText(body_text, "plain"))
    part = MIMEBase("application", "pdf")
    part.set_payload(attachment_bytes)
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", "attachment", filename=filename)
    msg.attach(part)
    return msg.as_bytes()


class FakeImapConn:
    """A minimal fake IMAP connection covering `login`/`select`/UID `FETCH`,
    returning a canned `(descriptor, raw_bytes)` tuple shaped like real
    `imaplib.IMAP4.uid()` — the same shape `tests/test_cr_oa_028_body_retrieval.py`'s
    `FakeImapConn` already uses. Returns the SAME canned raw message bytes for
    every FETCH regardless of the exact data-item spec requested, so this fake is
    agnostic to whether GREEN issues one combined fetch or a header + body pair."""

    def __init__(self, fetch_body):
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
            body = self.fetch_body
            return ("OK", [(f"1 (BODY[] {{{len(body)}}}".encode(), body), b")"])
        return ("OK", [b""])


def _imap_client_for(raw_bytes):
    fake = FakeImapConn(raw_bytes)
    adapter = GmailImapAdapter(
        account="gmail_test", source_tag="[GM]", host="imap.gmail.com",
        user="me@gmail.com", password="app-pw",
        conn_factory=lambda host, port: fake,
    )
    return MailClient({"gmail_test": adapter})


# ---------------------------------------------------------------------------
# JMAP fixtures
# ---------------------------------------------------------------------------

class _JmapFakeBodyTransport:
    """Records every `(method, url, headers, body)` call and returns canned
    `(status, dict)` tuples — no network. A canned GET session response, then a
    queue of canned POST `methodResponses` bodies consumed in order (the
    `tests/test_cr_oa_020_jmap.py` / `test_cr_oa_028_body_retrieval.py` pattern)."""

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

    def post_bodies(self):
        return [c[3] for c in self.calls if c[0] == "POST"]


def _jmap_full_item(uid, subject, sender_email, to_email,
                     text_body=None, html_body=None, attachments=None):
    item = {
        "id": uid,
        "threadId": "Ta1",
        "messageId": [f"<{uid}@example.com>"],
        "subject": subject,
        "from": [{"name": "Vendor", "email": sender_email}],
        "to": [{"name": "Alex Doe", "email": to_email}],
        "receivedAt": "2026-07-20T10:00:00Z",
        "textBody": [],
        "htmlBody": [],
        "bodyValues": {},
        "attachments": attachments or [],
    }
    if text_body is not None:
        item["textBody"] = [{"partId": "t1", "type": "text/plain"}]
        item["bodyValues"]["t1"] = {"value": text_body}
    if html_body is not None:
        item["htmlBody"] = [{"partId": "h1", "type": "text/html"}]
        item["bodyValues"]["h1"] = {"value": html_body}
    return item


def _jmap_email_get_response(item):
    return {
        "methodResponses": [
            ["Email/get",
             {"accountId": ACCOUNT_ID, "list": [item], "notFound": []},
             "0"],
        ],
    }


def _jmap_client_for(item):
    transport = _JmapFakeBodyTransport(responses=[_jmap_email_get_response(item)])
    adapter = JmapAdapter(
        account="fastmail_main", source_tag="[FM]", token="secret-token",
        session_url=SESSION_URL, transport=transport,
    )
    return MailClient({"fastmail_main": adapter}), transport, adapter


# ---------------------------------------------------------------------------
# §S1 AC 1/2/3/4 — IMAP half
# ---------------------------------------------------------------------------

class ImapMailGetBodyTest(unittest.TestCase):
    """§S1 ACs 1-4 (IMAP adapter). Fails today: `ImapAdapter.fetch_message`
    fetches headers only (see module docstring), so `payload.get("body")` is
    `None` and `payload.get("attachments")` is `None` in every case below —
    never the specific seeded value asserted."""

    def test_plain_text_message_body_matches_seeded_content(self):
        body_text = "Your order 12345 has shipped via FastShip, ETA 2026-08-02."
        raw = _build_plain_message(
            "Shipped", "orders@shop.example", "me@gmail.com", body_text)
        client = _imap_client_for(raw)

        payload, exit_code = _invoke_cmd_mail_get(client, "gmail_test", "600")

        self.assertIsNone(exit_code, f"unexpected error exit: {payload}")
        self.assertEqual(
            payload.get("body"), body_text,
            f"expected the seeded plain-text body, got {payload.get('body')!r}")

    def test_html_only_message_body_is_stripped_to_plain_text(self):
        html = "<html><body><p>Your parcel 88221 is out for delivery.</p></body></html>"
        raw = _build_html_only_message(
            "Out for delivery", "orders@shop.example", "me@gmail.com", html)
        client = _imap_client_for(raw)

        payload, exit_code = _invoke_cmd_mail_get(client, "gmail_test", "601")

        self.assertIsNone(exit_code, f"unexpected error exit: {payload}")
        body = payload.get("body") or ""
        self.assertIn("Your parcel 88221 is out for delivery.", body)
        self.assertNotIn("<", body, "HTML tags must be stripped, not passed through raw")

    def test_base64_encoded_body_is_returned_decoded(self):
        body_text = "Your café order 12345 shipped — ₹1,999 charged."
        raw = _build_encoded_plain_message(
            "Shipped", "orders@shop.example", "me@gmail.com", body_text, "base64")
        client = _imap_client_for(raw)

        payload, exit_code = _invoke_cmd_mail_get(client, "gmail_test", "602")

        self.assertIsNone(exit_code, f"unexpected error exit: {payload}")
        self.assertEqual(
            payload.get("body"), body_text,
            f"expected the DECODED body, got {payload.get('body')!r}")
        raw_b64_line = base64.encodebytes(
            body_text.encode("utf-8")).decode("ascii").splitlines()[0]
        self.assertNotIn(
            raw_b64_line, json.dumps(payload),
            "the raw base64 blob must never leak through undecoded")

    def test_quoted_printable_encoded_body_is_returned_decoded(self):
        body_text = "Your café order 12345 shipped."
        raw = _build_encoded_plain_message(
            "Shipped", "orders@shop.example", "me@gmail.com", body_text, "quoted-printable")
        client = _imap_client_for(raw)

        payload, exit_code = _invoke_cmd_mail_get(client, "gmail_test", "603")

        self.assertIsNone(exit_code, f"unexpected error exit: {payload}")
        self.assertEqual(
            payload.get("body"), body_text,
            f"expected the DECODED body, got {payload.get('body')!r}")
        self.assertNotIn(
            "=C3=A9", json.dumps(payload),
            "quoted-printable escape sequences must be decoded, not passed through raw")

    def test_attachment_listed_by_filename_and_size_with_no_bytes_in_payload(self):
        body_text = "Please find your receipt attached."
        attachment_bytes = b"%PDF-1.4 fake receipt content 0123456789"
        raw = _build_message_with_attachment(
            "Receipt", "billing@shop.example", "me@gmail.com", body_text,
            "receipt.pdf", attachment_bytes,
        )
        client = _imap_client_for(raw)

        payload, exit_code = _invoke_cmd_mail_get(client, "gmail_test", "604")

        self.assertIsNone(exit_code, f"unexpected error exit: {payload}")
        self.assertEqual(payload.get("body"), body_text)
        self.assertEqual(
            payload.get("attachments"),
            [{"filename": "receipt.pdf", "size": len(attachment_bytes)}],
            f"expected exactly one filename+size attachment entry, got {payload.get('attachments')!r}")
        rendered = json.dumps(payload)
        self.assertNotIn(
            attachment_bytes.decode("latin1"), rendered,
            "raw attachment bytes must never appear in the payload")
        encoded_attachment = base64.encodebytes(attachment_bytes).decode("ascii").strip()
        self.assertNotIn(
            encoded_attachment, rendered,
            "the attachment's base64 blob must never appear in the payload either")


# ---------------------------------------------------------------------------
# §S1 AC 1/2/3/4 — JMAP half
# ---------------------------------------------------------------------------

class JmapMailGetBodyTest(unittest.TestCase):
    """§S1 ACs 1-4 (JMAP adapter). Fails today: `JmapAdapter.fetch_message`
    requests only `_EMAIL_PROPERTIES` (see module docstring) — the canned
    `textBody`/`htmlBody`/`bodyValues`/`attachments` below are never read, so
    `payload.get("body")`/`payload.get("attachments")` stay `None`."""

    def test_plain_text_message_body_matches_seeded_content(self):
        body_text = "Your order 12345 has shipped via FastShip, ETA 2026-08-02."
        item = _jmap_full_item(
            "Ma1", "Shipped", "orders@vendor.example", "you@fastmail.com",
            text_body=body_text)
        client, _transport, _adapter = _jmap_client_for(item)

        payload, exit_code = _invoke_cmd_mail_get(client, "fastmail_main", "Ma1")

        self.assertIsNone(exit_code, f"unexpected error exit: {payload}")
        self.assertEqual(
            payload.get("body"), body_text,
            f"expected the seeded plain-text body, got {payload.get('body')!r}")

    def test_html_only_message_body_is_stripped_to_plain_text(self):
        html = "<html><body><p>Your parcel 88221 is out for delivery.</p></body></html>"
        item = _jmap_full_item(
            "Ma2", "Out for delivery", "orders@vendor.example", "you@fastmail.com",
            html_body=html)
        client, _transport, _adapter = _jmap_client_for(item)

        payload, exit_code = _invoke_cmd_mail_get(client, "fastmail_main", "Ma2")

        self.assertIsNone(exit_code, f"unexpected error exit: {payload}")
        body = payload.get("body") or ""
        self.assertIn("Your parcel 88221 is out for delivery.", body)
        self.assertNotIn("<", body, "HTML tags must be stripped, not passed through raw")

    def test_non_ascii_body_value_is_preserved_verbatim(self):
        """JMAP `bodyValues` arrive already MIME/charset-decoded by the server
        (RFC 8621 §4.1.4) — so §S1's charset-fidelity requirement is proven here
        by round-tripping genuine non-ASCII content through `fetch_message`
        untouched. (The transfer-encoding-decode half of AC3 is IMAP's, covered
        by `ImapMailGetBodyTest`'s base64/quoted-printable tests — a compliant
        JMAP server never hands a caller an undecoded transfer encoding.)"""
        body_text = "Your café order 12345 shipped — ₹1,999 charged."
        item = _jmap_full_item(
            "Ma3", "Shipped", "orders@vendor.example", "you@fastmail.com",
            text_body=body_text)
        client, _transport, _adapter = _jmap_client_for(item)

        payload, exit_code = _invoke_cmd_mail_get(client, "fastmail_main", "Ma3")

        self.assertIsNone(exit_code, f"unexpected error exit: {payload}")
        self.assertEqual(
            payload.get("body"), body_text,
            f"expected the non-ASCII body verbatim, got {payload.get('body')!r}")

    def test_attachment_listed_by_filename_and_size_with_no_bytes_in_payload(self):
        body_text = "Please find your receipt attached."
        attachments = [{"partId": "2", "blobId": "Gzz-1", "type": "application/pdf",
                         "name": "receipt.pdf", "size": 20480, "disposition": "attachment"}]
        item = _jmap_full_item(
            "Ma4", "Receipt", "billing@vendor.example", "you@fastmail.com",
            text_body=body_text, attachments=attachments)
        client, _transport, _adapter = _jmap_client_for(item)

        payload, exit_code = _invoke_cmd_mail_get(client, "fastmail_main", "Ma4")

        self.assertIsNone(exit_code, f"unexpected error exit: {payload}")
        self.assertEqual(payload.get("body"), body_text)
        self.assertEqual(
            payload.get("attachments"),
            [{"filename": "receipt.pdf", "size": 20480}],
            f"expected exactly one filename+size attachment entry, got {payload.get('attachments')!r}")
        rendered = json.dumps(payload)
        self.assertNotIn(
            "Gzz-1", rendered,
            "the JMAP blobId (a content reference, not attachment bytes) must not leak into the payload")


# ---------------------------------------------------------------------------
# §S1 folded-in AC (CR-OA-030 VERIFY) — JMAP method-level error attribution
# ---------------------------------------------------------------------------

def _jmap_method_error_response(error_type, description):
    return {
        "methodResponses": [
            ["error", {"type": error_type, "description": description}, "0"],
        ],
    }


class JmapFetchMessageSurfacesMethodErrorTest(unittest.TestCase):
    """§S1 folded-in AC: a method-level `Email/get` error must not be collapsed
    to `fetch_message` returning `None` (which `cmd_mail_get` then reports as the
    misleading "message not found"). Fails today: `_email_get_list`
    (`vidushi_oa/mail/jmap.py:561-566`) does not route through the shared
    `_raise_for_method_error` CR-OA-030 introduced — it just scans for an
    `Email/get` response, finds none, and returns `[]`, so `fetch_message` returns
    `None` and the real `forbidden`/description is lost."""

    def test_jmap_fetch_message_raises_with_the_servers_error_type_and_description(self):
        transport = _JmapFakeBodyTransport(responses=[_jmap_method_error_response(
            "forbidden", "Insufficient permission to access this mailbox")])
        adapter = JmapAdapter(
            account="fastmail_main", source_tag="[FM]", token="secret-token",
            session_url=SESSION_URL, transport=transport,
        )

        with self.assertRaises(RuntimeError) as ctx:
            adapter.fetch_message("Ma1")

        message = str(ctx.exception)
        self.assertIn("forbidden", message, f"server error type missing from: {message!r}")
        self.assertIn(
            "Insufficient permission to access this mailbox", message,
            f"server error description missing from: {message!r}")

    def test_cmd_mail_get_surfaces_the_real_jmap_server_error_not_message_not_found(self):
        transport = _JmapFakeBodyTransport(responses=[_jmap_method_error_response(
            "forbidden", "Insufficient permission to access this mailbox")])
        adapter = JmapAdapter(
            account="fastmail_main", source_tag="[FM]", token="secret-token",
            session_url=SESSION_URL, transport=transport,
        )
        client = MailClient({"fastmail_main": adapter})

        payload, exit_code = _invoke_cmd_mail_get(client, "fastmail_main", "Ma1")

        self.assertEqual(exit_code, 1, f"expected exit 1, got {exit_code} (payload={payload})")
        rendered = json.dumps(payload)
        self.assertIn("forbidden", rendered, f"real server error type missing from payload: {payload}")
        self.assertIn(
            "Insufficient permission to access this mailbox", rendered,
            f"real server error description missing from payload: {payload}")
        self.assertNotIn(
            "message not found", rendered,
            f"must not misattribute a server error as a missing message: {payload}")


# ---------------------------------------------------------------------------
# §S1 folded-in AC (CR-OA-030 VERIFY) — stale docstring
# ---------------------------------------------------------------------------

class CmdMailGetDocstringTest(unittest.TestCase):
    """§S1 folded-in AC: `cmd_mail_get`'s docstring no longer claims
    `JmapAdapter` raises `NotImplementedError` for mail-get — superseded by
    CR-OA-028's real `fetch_message`. Fails today: the docstring
    (`vidushi_oa/_cli.py` ~lines 907-908) literally states
    "`JmapAdapter` raises `NotImplementedError`."."""

    def test_docstring_does_not_claim_jmapadapter_raises_notimplementederror(self):
        doc = inspect.getdoc(cli.cmd_mail_get) or ""
        self.assertNotIn(
            "NotImplementedError", doc,
            f"stale docstring claim survives in cmd_mail_get.__doc__: {doc!r}")


if __name__ == "__main__":
    unittest.main()
