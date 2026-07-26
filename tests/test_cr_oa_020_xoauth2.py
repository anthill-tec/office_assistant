"""CR-OA-020 §S6 — Gmail Workspace XOAUTH2 IMAP fallback (RED).

`vidushi_oa/mail/xoauth2.py` does not exist yet, so the import below fails with
`ModuleNotFoundError` until GREEN lands:

  - `build_xoauth2_string(user, access_token) -> bytes` — the SASL `XOAUTH2`
    string, base64-encoded: `base64.b64encode(
    ("user=%s\x01auth=Bearer %s\x01\x01" % (user, access_token)).encode())`.
  - `refresh_access_token(client_id, client_secret, refresh_token,
    transport=None, token_url=...) -> str` — POSTs
    `grant_type=refresh_token` + the creds via an injectable
    `transport(method, url, headers, body) -> (status, dict)`, returns the
    `access_token` field of the decoded JSON response. No `httpx` — a minimal
    stdlib-`urllib` transport is the (untested-here) default.
  - `GmailXoauth2Adapter(GmailImapAdapter)` — takes an `access_token` instead
    of a `password`; `_conn()` authenticates via
    `conn.authenticate("XOAUTH2", <callable>)` (NOT `.login()`). Because
    `imaplib.IMAP4.authenticate()` base64-encodes whatever the callable
    returns before sending it over the wire, the callable must return the
    RAW (decoded) SASL bytes — `base64.b64decode(build_xoauth2_string(user,
    access_token))` — NOT the already-base64 `build_xoauth2_string(...)`
    value itself (that would make imaplib double-encode it). Then selects
    `INBOX`. Cached/reused exactly like the base `ImapAdapter`.

No real IMAP/network — `FakeXoauthIMAP` records `.authenticate(mechanism,
callback)` (calling `callback(b"")` once and capturing what it returns),
`.select`, and `.login` (asserted NEVER called on the XOAUTH2 path). No real
HTTP — `refresh_access_token`'s `transport` is a plain injected callable
capturing `(method, url, headers, body)`.
"""
import base64
import unittest

from vidushi_oa.mail.xoauth2 import (
    GmailXoauth2Adapter,
    build_xoauth2_string,
    refresh_access_token,
)


class FakeXoauthIMAP:
    """Records `.authenticate(mechanism, callback)` — calls `callback(b"")`
    once (mirroring imaplib handing the authobject the server's initial
    continuation response) and captures the bytes it returns — plus `.select`
    and `.login` (must stay untouched on the XOAUTH2 path)."""

    def __init__(self, authenticate_response=None, select_response=None,
                 list_response=None):
        self.authenticate_response = (
            authenticate_response if authenticate_response is not None
            else ("OK", [b"Success"])
        )
        self.select_response = select_response if select_response is not None else ("OK", [b"1"])
        self.list_response = list_response if list_response is not None else (
            "OK", [b'(\\HasNoChildren) "/" "INBOX"'],
        )
        self.authenticate_calls = []
        self.select_calls = []
        self.login_calls = []
        self.list_calls = 0

    def authenticate(self, mechanism, callback):
        returned = callback(b"")
        self.authenticate_calls.append((mechanism, returned))
        return self.authenticate_response

    def select(self, mailbox="INBOX", readonly=False):
        self.select_calls.append(mailbox)
        return self.select_response

    def login(self, user, password):
        self.login_calls.append((user, password))
        return ("OK", [b"Logged in"])

    def list(self, *args):
        self.list_calls += 1
        return self.list_response


def _make_conn_factory(fake):
    """Returns (factory, calls) — `calls` records every (host, port) the
    adapter asked the factory to build a connection for."""
    calls = []

    def factory(host, port):
        calls.append((host, port))
        return fake

    return factory, calls


def _make_fake_transport(response):
    """Returns (transport, calls) — a fake `transport(method, url, headers,
    body)` capturing every call and returning the canned `(status, dict)`."""
    calls = []

    def transport(method, url, headers, body):
        calls.append((method, url, headers, body))
        return response

    return transport, calls


def _body_text(body):
    return body.decode() if isinstance(body, bytes) else body


class BuildXoauth2StringTest(unittest.TestCase):
    """§S6 AC: given an access token, the adapter builds the correct base64
    `user=...\x01auth=Bearer ...\x01\x01` SASL string."""

    def test_encodes_the_exact_sasl_string_for_a_known_user_and_token(self):
        expected = base64.b64encode(
            "user=user@x.com\x01auth=Bearer tok123\x01\x01".encode()
        )

        result = build_xoauth2_string("user@x.com", "tok123")

        self.assertEqual(result, expected)

    def test_different_user_or_token_changes_the_encoded_string(self):
        base = build_xoauth2_string("user@x.com", "tok123")

        different_user = build_xoauth2_string("other@x.com", "tok123")
        different_token = build_xoauth2_string("user@x.com", "other-tok")

        self.assertNotEqual(base, different_user)
        self.assertNotEqual(base, different_token)


class GmailXoauth2AdapterAuthenticatesTest(unittest.TestCase):
    """§S6 AC: creating the adapter and triggering `_conn()` calls
    `conn.authenticate("XOAUTH2", cb)` with the correct SASL bytes from the
    callback, and NEVER calls `conn.login()`."""

    def setUp(self):
        self.fake = FakeXoauthIMAP()
        self.factory, self.factory_calls = _make_conn_factory(self.fake)
        self.adapter = GmailXoauth2Adapter(
            account="gmail_work",
            source_tag="[GM]",
            host="imap.gmail.com",
            user="me@workspace.example",
            access_token="access-tok-1",
            conn_factory=self.factory,
        )

    def test_list_folders_authenticates_via_xoauth2_mechanism(self):
        self.adapter.list_folders()

        self.assertEqual(len(self.fake.authenticate_calls), 1)
        mechanism, _ = self.fake.authenticate_calls[0]
        self.assertEqual(mechanism, "XOAUTH2")

    def test_authenticate_callback_returns_the_correct_sasl_bytes(self):
        # imaplib.IMAP4.authenticate() base64-encodes whatever the authobject
        # callback returns before sending it to the server. The callback must
        # therefore hand back the RAW (decoded) SASL bytes, NOT the already
        # base64-encoded `build_xoauth2_string(...)` value — returning the
        # base64 form here would make imaplib double-encode it, which a real
        # Gmail IMAP server would reject.
        self.adapter.list_folders()

        expected_raw = base64.b64decode(
            build_xoauth2_string("me@workspace.example", "access-tok-1")
        )
        _, returned_bytes = self.fake.authenticate_calls[0]
        self.assertEqual(returned_bytes, expected_raw)

    def test_login_is_never_called_on_the_xoauth2_path(self):
        self.adapter.list_folders()

        self.assertEqual(self.fake.login_calls, [])

    def test_selects_inbox_after_authenticating(self):
        self.adapter.list_folders()

        self.assertIn("INBOX", self.fake.select_calls)


class GmailXoauth2AdapterConnectionReuseTest(unittest.TestCase):
    """§S6 groundwork (consistent with the base `ImapAdapter`): the connection
    is created and authenticated at most once across multiple operations."""

    def test_two_operations_create_and_authenticate_the_connection_exactly_once(self):
        fake = FakeXoauthIMAP()
        factory, factory_calls = _make_conn_factory(fake)
        adapter = GmailXoauth2Adapter(
            account="gmail_work", source_tag="[GM]", host="imap.gmail.com",
            user="me@workspace.example", access_token="access-tok-1",
            conn_factory=factory,
        )

        adapter.list_folders()
        adapter.list_folders()

        self.assertEqual(len(factory_calls), 1)
        self.assertEqual(len(fake.authenticate_calls), 1)
        self.assertEqual(fake.login_calls, [])


class RefreshAccessTokenTest(unittest.TestCase):
    """§S6 groundwork: `refresh_access_token` posts a `grant_type=refresh_token`
    request (with the client id/secret/refresh token) via the injected
    `transport` and returns the decoded `access_token`."""

    def test_returns_the_access_token_from_a_successful_refresh(self):
        transport, calls = _make_fake_transport(
            (200, {"access_token": "newtok", "expires_in": 3599})
        )

        token = refresh_access_token(
            client_id="client-123",
            client_secret="secret-abc",
            refresh_token="refresh-xyz",
            transport=transport,
        )

        self.assertEqual(token, "newtok")
        self.assertEqual(len(calls), 1)

    def test_issues_exactly_one_post_to_the_default_google_token_endpoint(self):
        transport, calls = _make_fake_transport((200, {"access_token": "newtok"}))

        refresh_access_token(
            client_id="client-123",
            client_secret="secret-abc",
            refresh_token="refresh-xyz",
            transport=transport,
        )

        self.assertEqual(len(calls), 1)
        method, url, _headers, _body = calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://oauth2.googleapis.com/token")

    def test_post_body_carries_grant_type_refresh_token_and_the_refresh_token(self):
        transport, calls = _make_fake_transport((200, {"access_token": "newtok"}))

        refresh_access_token(
            client_id="client-123",
            client_secret="secret-abc",
            refresh_token="refresh-xyz",
            transport=transport,
        )

        _method, _url, _headers, body = calls[0]
        body_text = _body_text(body)
        self.assertIn("grant_type=refresh_token", body_text)
        self.assertIn("refresh-xyz", body_text)

    def test_custom_token_url_is_honoured(self):
        transport, calls = _make_fake_transport((200, {"access_token": "newtok"}))

        refresh_access_token(
            client_id="client-123",
            client_secret="secret-abc",
            refresh_token="refresh-xyz",
            transport=transport,
            token_url="https://example.test/token",
        )

        _method, url, _headers, _body = calls[0]
        self.assertEqual(url, "https://example.test/token")


if __name__ == "__main__":
    unittest.main()
