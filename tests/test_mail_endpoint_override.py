"""DN-mail-e2e-emulator-testing.md §"Decision 3 — PREREQUISITE: configurable
provider endpoints" (RED).

Every mail adapter is hardcoded to a real provider today, so the engine cannot
be pointed at a local emulator (Stalwart) for E2E testing:

  - `JmapAdapter.__init__` defaults `session_url` to the real Fastmail session
    URL, and `fastmail_adapter()` never overrides it.
  - `fastmail_adapter`'s IMAP fallback hardcodes `host="imap.fastmail.com"`;
    `vidushi_oa.mail.factory._default_adapter_factory` hardcodes
    `imap.gmail.com` / `imap.mail.yahoo.com` for the Gmail/Yahoo branches and
    forwards neither an `endpoint` nor a `conn_factory` to the adapters it
    builds.
  - The account registry (`vidushi_oa/mail/accounts.py`) carries no `endpoint`
    field, and `voa mail-auth` has no flag to set one.
  - Nothing anywhere consults a `VIDUSHI_MAIL_ENDPOINTS` env var.

This file pins the DN's specification: an optional per-account `endpoint`
object (`jmap_url` / `imap_host` / `imap_port` / `smtp_host` / `smtp_port`,
each optional) that overrides the provider default when set, and changes
NOTHING for a real account when absent. Every test below is RED against
today's code for one of two legitimate reasons:

  - a behavioural assertion fails outright (the override has no effect yet), or
  - a call passes an `endpoint`/`conn_factory` keyword that
    `accounts.add_account` / `_default_adapter_factory` do not accept yet
    -> `TypeError`.

No production code is touched here — only the specification, expressed as
tests. No real network anywhere: JMAP is faked via an injected transport
callable, IMAP/SMTP via an injected `conn_factory` / a patched
`vidushi_oa.mail.imap.smtplib.SMTP`.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from vidushi_oa.mail import accounts
from vidushi_oa.mail.compose import compose
from vidushi_oa.mail.factory import _default_adapter_factory, build_client
from vidushi_oa.mail.imap import ImapAdapter
from vidushi_oa.mail.jmap import JmapAdapter, fastmail_adapter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STORE = os.path.join(ROOT, "scripts", "store.py")

FASTMAIL_DEFAULT_SESSION_URL = "https://api.fastmail.com/jmap/session"


class FakeResolver:
    """Deterministic stand-in for `SecretResolver` — no keyring/file I/O
    (same shape as `tests/test_cr_oa_020_factory.py`'s)."""

    def resolve(self, ref):
        return f"secret-for-{ref}"


class _RecordingTransport:
    """A JMAP transport that answers the session GET with a canned document and
    an empty `Email/get` list for anything else, recording every
    `(method, url)` it was called with — so a test can prove WHICH url the
    session was actually fetched from, not merely inspect an attribute."""

    def __init__(self, session_response=None):
        self.session_response = session_response or {
            "apiUrl": "http://placeholder.invalid/api/",
            "primaryAccounts": {"urn:ietf:params:jmap:mail": "u1"},
        }
        self.urls_called = []

    def __call__(self, method, url, headers, body):
        self.urls_called.append((method, url))
        if method == "GET":
            return 200, self.session_response
        return 200, {"methodResponses": [["Email/get", {"list": []}, "1"]]}


class _FakeImapConn:
    """Minimal fake IMAP connection for the endpoint-override tests: only what
    a `search()`/`send_draft()` call needs to complete without a real socket
    (login/select/uid SEARCH+FETCH+STORE, append, and a LIST advertising the
    RFC 6154 Drafts/Sent special-use attributes so `send_draft`'s post-delivery
    bookkeeping resolves cleanly)."""

    _LIST = [
        b'(\\HasNoChildren \\Drafts) "/" "Drafts"',
        b'(\\HasNoChildren \\Sent) "/" "Sent"',
    ]

    def __init__(self, fetch_body=None):
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
        return ("OK", [b"[APPENDUID 1 900] (Success)"])

    def list(self, directory='""', pattern="*"):
        return ("OK", list(self._LIST))

    def uid(self, command, *args):
        self.uid_calls.append((command, args))
        if command == "SEARCH":
            return ("OK", [b""])
        if command == "FETCH":
            uid = args[0]
            body = self.fetch_body or b""
            return ("OK", [(f"{uid} (BODY[] {{{len(body)}}}".encode(), body), b")"])
        if command == "STORE":
            return ("OK", [b"Completed"])
        return ("OK", [b""])


# ---------------------------------------------------------------------------
# Item 1 — account registry round-trip
# ---------------------------------------------------------------------------

def test_add_account_round_trips_full_endpoint_override(tmp_path):
    config_path = tmp_path / "accounts.json"
    endpoint = {
        "jmap_url": "http://emu.local:8080/jmap/session",
        "imap_host": "emu.local",
        "imap_port": 1143,
        "smtp_host": "emu.local",
        "smtp_port": 1587,
    }

    accounts.add_account("fastmail_emu", "fastmail", "me@emu.test", "ref-1",
                         endpoint=endpoint, path=str(config_path))

    reloaded = accounts.load_accounts(str(config_path))
    assert len(reloaded) == 1
    assert reloaded[0]["endpoint"] == endpoint


def test_add_account_endpoint_defaults_to_empty_when_not_configured(tmp_path):
    config_path = tmp_path / "accounts.json"

    entry = accounts.add_account("fastmail_real", "fastmail", "me@fastmail.com",
                                 "ref-2", endpoint=None, path=str(config_path))

    assert not entry.get("endpoint"), (
        f"an account with no endpoint override must not fabricate one: {entry!r}")
    reloaded = accounts.load_accounts(str(config_path))
    assert not reloaded[0].get("endpoint")


# ---------------------------------------------------------------------------
# Item 2 — JMAP session_url override
# ---------------------------------------------------------------------------

def test_fastmail_adapter_uses_endpoint_jmap_url_as_session_url_and_fetches_it():
    override_url = "http://emu.local:8080/jmap/session"
    transport = _RecordingTransport()
    config = {"jmap_token": "tok", "username": "me@fastmail.com",
             "endpoint": {"jmap_url": override_url}}

    adapter = fastmail_adapter("fastmail_emu", "[FM]", config, transport=transport)
    assert adapter.session_url == override_url, (
        "fastmail_adapter must build the JmapAdapter with the configured "
        "endpoint.jmap_url as its session_url")

    adapter.search("invoice")

    assert ("GET", override_url) in transport.urls_called, (
        f"the session must actually be fetched from the override url; "
        f"got {transport.urls_called!r}")
    assert ("GET", FASTMAIL_DEFAULT_SESSION_URL) not in transport.urls_called


# ---------------------------------------------------------------------------
# Item 3 — IMAP host/port override
# ---------------------------------------------------------------------------

def test_default_adapter_factory_connects_gmail_to_the_endpoint_override_host_and_port():
    fake_conn = _FakeImapConn()
    calls = []

    def conn_factory(host, port):
        calls.append((host, port))
        return fake_conn

    adapter = _default_adapter_factory(
        provider="gmail", account="g_emu", address="me@emu.test",
        secret_ref="r1", resolver=FakeResolver(),
        endpoint={"imap_host": "emu.local", "imap_port": 1143},
        conn_factory=conn_factory,
    )

    adapter.search("test")

    assert calls == [("emu.local", 1143)], (
        f"search() must connect to the endpoint override host/port; got {calls!r}")


def test_default_adapter_factory_connects_yahoo_to_the_endpoint_override_host_and_port():
    fake_conn = _FakeImapConn()
    calls = []

    def conn_factory(host, port):
        calls.append((host, port))
        return fake_conn

    adapter = _default_adapter_factory(
        provider="yahoo", account="y_emu", address="me@emu.test",
        secret_ref="r1", resolver=FakeResolver(),
        endpoint={"imap_host": "emu.local", "imap_port": 1144},
        conn_factory=conn_factory,
    )

    adapter.search("test")

    assert calls == [("emu.local", 1144)], (
        f"search() must connect to the endpoint override host/port; got {calls!r}")


def test_default_adapter_factory_connects_to_the_real_provider_host_when_endpoint_absent():
    fake_conn = _FakeImapConn()
    calls = []

    def conn_factory(host, port):
        calls.append((host, port))
        return fake_conn

    adapter = _default_adapter_factory(
        provider="gmail", account="g_real", address="me@gmail.com",
        secret_ref="r1", resolver=FakeResolver(),
        endpoint=None, conn_factory=conn_factory,
    )

    adapter.search("test")

    assert calls == [("imap.gmail.com", 993)], (
        f"with no endpoint override the real provider host/port must be dialed; "
        f"got {calls!r}")


def test_fastmail_adapter_imap_fallback_uses_endpoint_host_and_port_override():
    config = {"app_password": "app-pw", "username": "me@fastmail.com",
             "endpoint": {"imap_host": "emu.local", "imap_port": 1143}}

    adapter = fastmail_adapter("fastmail_emu_imap", "[FM]", config)

    assert isinstance(adapter, ImapAdapter)
    assert adapter.host == "emu.local", (
        f"the Fastmail IMAP fallback must honour endpoint.imap_host; "
        f"got {adapter.host!r}")
    assert adapter.port == 1143


# ---------------------------------------------------------------------------
# Item 4 — SMTP host/port override on send_draft
# ---------------------------------------------------------------------------

class SendDraftEndpointOverrideTest(unittest.TestCase):
    def _draft_bytes(self, from_addr="me@emu.test"):
        return compose(from_addr=from_addr, to="support@example.com",
                       subject="Return request", body="Requesting an RMA.")

    def test_send_draft_uses_endpoint_smtp_host_and_port_override(self):
        fake_imap = _FakeImapConn(fetch_body=self._draft_bytes())
        adapter = _default_adapter_factory(
            provider="yahoo", account="y_emu_smtp", address="me@emu.test",
            secret_ref="r1", resolver=FakeResolver(),
            endpoint={"smtp_host": "emu.local", "smtp_port": 1587},
            conn_factory=lambda host, port: fake_imap,
        )

        with patch("vidushi_oa.mail.imap.smtplib.SMTP") as smtp_cls:
            smtp_cls.return_value.sendmail.return_value = {}
            adapter.send_draft("901")

        smtp_cls.assert_called_once_with("emu.local", 1587)

    def test_send_draft_uses_the_derived_default_smtp_host_when_endpoint_absent(self):
        fake_imap = _FakeImapConn(fetch_body=self._draft_bytes())
        adapter = _default_adapter_factory(
            provider="yahoo", account="y_real_smtp", address="me@yahoo.com",
            secret_ref="r1", resolver=FakeResolver(),
            endpoint=None,
            conn_factory=lambda host, port: fake_imap,
        )

        with patch("vidushi_oa.mail.imap.smtplib.SMTP") as smtp_cls:
            smtp_cls.return_value.sendmail.return_value = {}
            adapter.send_draft("901")

        smtp_cls.assert_called_once_with("smtp.mail.yahoo.com", 587)


# ---------------------------------------------------------------------------
# Item 5 — VIDUSHI_MAIL_ENDPOINTS process-level env override
# ---------------------------------------------------------------------------

def test_vidushi_mail_endpoints_env_var_overrides_only_when_explicitly_set(
        tmp_path, monkeypatch):
    config_path = tmp_path / "accounts.json"
    accounts.add_account("fastmail_real", "fastmail", "me@fastmail.com", "ref-f",
                         path=str(config_path))

    monkeypatch.setenv("VIDUSHI_MAIL_ENDPOINTS", json.dumps({
        "fastmail_real": {"jmap_url": "http://emu.local:8080/jmap/session"},
    }))
    overridden_client = build_client(config_path=str(config_path), resolver=FakeResolver())
    overridden_adapter = overridden_client._adapters["fastmail_real"]
    assert overridden_adapter.session_url == "http://emu.local:8080/jmap/session", (
        "VIDUSHI_MAIL_ENDPOINTS must override the account's endpoint when set")

    monkeypatch.delenv("VIDUSHI_MAIL_ENDPOINTS", raising=False)
    default_client = build_client(config_path=str(config_path), resolver=FakeResolver())
    default_adapter = default_client._adapters["fastmail_real"]
    assert default_adapter.session_url == FASTMAIL_DEFAULT_SESSION_URL, (
        "with VIDUSHI_MAIL_ENDPOINTS unset nothing must change for a real account")


# ---------------------------------------------------------------------------
# Item 6 — `voa mail-auth --endpoint`
# ---------------------------------------------------------------------------

class MailAuthEndpointFlagTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="oa-endpoint-override-")
        self.accounts_path = os.path.join(self.tmp, "accounts.json")
        self.secrets_path = os.path.join(self.tmp, "secrets.json")
        self.env = dict(os.environ)
        self.env["VIDUSHI_MAIL_CONFIG"] = self.accounts_path
        self.env["VIDUSHI_SECRETS_FILE"] = self.secrets_path
        self.env["VIDUSHI_SECRET_BACKEND"] = "file"
        self.env["VIDUSHI_FORMAT"] = "json"
        self.env.pop("PYTHON_KEYRING_BACKEND", None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_mail_auth_persists_endpoint_override_from_cli_flag(self):
        endpoint_json = json.dumps({
            "jmap_url": "http://emu.local:8080/jmap/session",
            "imap_host": "emu.local",
            "imap_port": 1143,
        })
        r = subprocess.run(
            [sys.executable, STORE, "mail-auth", "--provider", "fastmail",
             "--address", "me@emu.test", "--endpoint", endpoint_json],
            capture_output=True, text=True, env=self.env, input="secretval\n",
        )
        self.assertEqual(r.returncode, 0,
                         f"mail-auth --endpoint must be accepted; "
                         f"stdout={r.stdout!r} stderr={r.stderr!r}")

        with open(self.accounts_path, encoding="utf-8") as f:
            entries = json.load(f)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].get("endpoint"), {
            "jmap_url": "http://emu.local:8080/jmap/session",
            "imap_host": "emu.local",
            "imap_port": 1143,
        })

    def test_mail_auth_help_lists_endpoint_flag(self):
        r = subprocess.run([sys.executable, STORE, "mail-auth", "--help"],
                           capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--endpoint", r.stdout,
                      f"mail-auth --help must list --endpoint; got:\n{r.stdout}")


# ---------------------------------------------------------------------------
# Item 7 — invariant: no behaviour change for a real account
# ---------------------------------------------------------------------------

def test_no_endpoint_override_resolves_to_real_provider_defaults_across_all_adapters():
    gmail = _default_adapter_factory(
        provider="gmail", account="g1", address="me@gmail.com",
        secret_ref="r1", resolver=FakeResolver(), endpoint=None)
    yahoo = _default_adapter_factory(
        provider="yahoo", account="y1", address="me@yahoo.com",
        secret_ref="r2", resolver=FakeResolver(), endpoint=None)
    fastmail = _default_adapter_factory(
        provider="fastmail", account="f1", address="me@fastmail.com",
        secret_ref="r3", resolver=FakeResolver(), endpoint=None)

    assert gmail.host == "imap.gmail.com"
    assert gmail.port == 993
    assert yahoo.host == "imap.mail.yahoo.com"
    assert yahoo.port == 993
    assert isinstance(fastmail, JmapAdapter)
    assert fastmail.session_url == FASTMAIL_DEFAULT_SESSION_URL


if __name__ == "__main__":
    unittest.main()
