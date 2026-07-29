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

import pytest

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


class _FixedResolver:
    """`SecretResolver` stand-in returning ONE canned secret — the XOAUTH2 path
    needs a `{client_id, client_secret, refresh_token}` JSON blob, not the
    `FakeResolver` sentinel string."""

    def __init__(self, secret):
        self.secret = secret

    def resolve(self, ref):
        return self.secret


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
        # Pin the store into the same throwaway dir: `doctor` reads the active
        # backend, and the suite-wide Mongo pin would otherwise reach a real server.
        self.env["VIDUSHI_BACKEND"] = "sqlite"
        self.env["VIDUSHI_SQLITE_PATH"] = os.path.join(self.tmp, "oa.db")
        self.env["VIDUSHI_DATA_DIR"] = self.tmp
        self.env.pop("PYTHON_KEYRING_BACKEND", None)
        # An override inherited from the developer's own shell would silently rewrite
        # every endpoint these cases assert on; each test that wants one sets it.
        self.env.pop("VIDUSHI_MAIL_ENDPOINTS", None)

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

    def _mail_auth(self, *args, stdin=None):
        return subprocess.run(
            [sys.executable, STORE, "mail-auth", *args],
            capture_output=True, text=True, env=self.env, input=stdin)

    def _entry(self):
        with open(self.accounts_path, encoding="utf-8") as f:
            entries = json.load(f)
        self.assertEqual(len(entries), 1, f"expected one entry; got {entries!r}")
        return entries[0]

    def test_clearing_tls_verify_per_doctor_preserves_send_aliases_and_auth_mode(self):
        """`doctor`'s TLS remediation re-registers the account to drop a
        `tls_verify: false` override. `add_account` replaces the matched entry
        WHOLESALE, so a re-registration that does not re-state `--send` / `--alias` /
        `--auth-mode` must carry the stored ones forward — otherwise following
        doctor's own advice silently revokes send capability, wipes every alias, and
        resets an XOAUTH2 Gmail account to `password` (which then builds a plain
        `GmailImapAdapter`)."""
        r = self._mail_auth(
            "--provider", "gmail", "--address", "me@emu.test",
            "--secret-ref", "vidushi-oa/gmail:me@emu.test",
            "--auth-mode", "xoauth2", "--send",
            "--alias", "vendor.alias@emu.test", "--alias", "second@emu.test",
            "--endpoint", json.dumps({"imap_host": "emu.local", "imap_port": 1993,
                                      "tls_verify": False}))
        self.assertEqual(r.returncode, 0, f"stdout={r.stdout!r} stderr={r.stderr!r}")

        # Exactly what doctor's remediation step emits: the endpoint minus
        # `tls_verify`, plus the account's own --secret-ref, and nothing else.
        r = self._mail_auth(
            "--provider", "gmail", "--address", "me@emu.test",
            "--secret-ref", "vidushi-oa/gmail:me@emu.test",
            "--endpoint", json.dumps({"imap_host": "emu.local", "imap_port": 1993}))
        self.assertEqual(r.returncode, 0, f"stdout={r.stdout!r} stderr={r.stderr!r}")

        entry = self._entry()
        self.assertEqual(entry.get("endpoint"),
                         {"imap_host": "emu.local", "imap_port": 1993},
                         f"the tls_verify opt-out must be gone: {entry!r}")
        self.assertTrue(entry.get("send"),
                        f"re-registration must not revoke send capability: {entry!r}")
        self.assertEqual(entry.get("aliases"),
                         ["vendor.alias@emu.test", "second@emu.test"],
                         f"re-registration must not wipe configured aliases: {entry!r}")
        self.assertEqual(entry.get("auth_mode"), "xoauth2",
                         f"re-registration must not reset the auth-mode: {entry!r}")

    def test_send_capability_is_revocable_with_no_send_and_preserved_without_it(self):
        """Carrying `send` forward must not make the grant monotonic:
        `send_gate.ensure_send_capable` is the ONLY outbound-dispatch guard, so a
        capability the CLI can grant but never take back would leave revoking to a
        hand-edit of the registry. `--no-send` is the explicit counterpart; omitting
        both flags still preserves whatever the account had."""
        args = ("--provider", "gmail", "--address", "me@emu.test",
                "--secret-ref", "vidushi-oa/gmail:me@emu.test",
                "--alias", "vendor.alias@emu.test")
        r = self._mail_auth(*args, "--send")
        self.assertEqual(r.returncode, 0, f"stdout={r.stdout!r} stderr={r.stderr!r}")
        self.assertTrue(self._entry().get("send"))

        r = self._mail_auth(*args)
        self.assertEqual(r.returncode, 0, f"stdout={r.stdout!r} stderr={r.stderr!r}")
        self.assertTrue(
            self._entry().get("send"),
            "a re-registration naming neither flag must preserve the send grant")

        r = self._mail_auth(*args, "--no-send")
        self.assertEqual(r.returncode, 0, f"stdout={r.stdout!r} stderr={r.stderr!r}")
        entry = self._entry()
        self.assertFalse(
            entry.get("send"),
            f"--no-send must revoke send capability from the CLI: {entry!r}")
        self.assertEqual(
            entry.get("aliases"), ["vendor.alias@emu.test"],
            f"revoking send must not disturb the rest of the account: {entry!r}")

        r = self._mail_auth(*args)
        self.assertEqual(r.returncode, 0, f"stdout={r.stdout!r} stderr={r.stderr!r}")
        self.assertFalse(
            self._entry().get("send"),
            "a re-registration naming neither flag must preserve the revocation too")

    def test_send_and_no_send_are_mutually_exclusive(self):
        r = self._mail_auth(
            "--provider", "gmail", "--address", "me@emu.test",
            "--secret-ref", "vidushi-oa/gmail:me@emu.test", "--send", "--no-send")
        self.assertNotEqual(r.returncode, 0,
                            "--send and --no-send must not be accepted together")

    def test_doctor_reports_the_per_account_send_grant(self):
        r = self._mail_auth(
            "--provider", "gmail", "--address", "me@emu.test",
            "--secret-ref", "vidushi-oa/gmail:me@emu.test", "--send")
        self.assertEqual(r.returncode, 0, f"stdout={r.stdout!r} stderr={r.stderr!r}")

        r = subprocess.run([sys.executable, STORE, "doctor"],
                           capture_output=True, text=True, env=self.env)
        row = json.loads(r.stdout)["accounts"][0]
        self.assertTrue(
            row.get("send"),
            f"doctor must surface the per-account send grant — `mail-accounts` only "
            f"reports the adapter class's static capability set; got {row!r}")

    def test_doctor_tls_remediation_names_a_command_that_preserves_the_account(self):
        r = self._mail_auth(
            "--provider", "gmail", "--address", "me@emu.test",
            "--secret-ref", "vidushi-oa/gmail:me@emu.test", "--send",
            "--endpoint", json.dumps({"imap_host": "emu.local", "tls_verify": False}))
        self.assertEqual(r.returncode, 0, f"stdout={r.stdout!r} stderr={r.stderr!r}")

        r = subprocess.run([sys.executable, STORE, "doctor"],
                           capture_output=True, text=True, env=self.env)
        payload = json.loads(r.stdout)
        row = payload["accounts"][0]
        self.assertFalse(row["tls_verify"],
                         f"doctor must surface a disabled TLS verification: {row!r}")
        steps = [s["step"] for s in payload["remediation"]
                 if "tls_verify" in s["step"]]
        self.assertEqual(len(steps), 1, f"expected one TLS step: {payload!r}")
        step = steps[0]
        self.assertIn("--secret-ref vidushi-oa/gmail:me@emu.test", step,
                      f"the suggested command must pass the account's existing "
                      f"secret-ref so the stored credential is never re-read from "
                      f"stdin; got {step!r}")
        self.assertIn('--endpoint \'{"imap_host": "emu.local"}\'', step,
                      f"the suggested endpoint must drop ONLY tls_verify, keeping "
                      f"every other configured key; got {step!r}")

    def test_doctor_reports_the_endpoint_the_adapters_are_actually_built_with(self):
        """`build_client` layers `VIDUSHI_MAIL_ENDPOINTS` on top of the stored
        endpoint before building any adapter, so a diagnostic that reads only the
        stored one describes a channel nobody dials: an account whose TLS
        verification the environment has switched OFF (or whose host it has
        repointed) would read exactly like a hardened one."""
        r = self._mail_auth(
            "--provider", "gmail", "--address", "me@emu.test",
            "--secret-ref", "vidushi-oa/gmail:me@emu.test", "--send")
        self.assertEqual(r.returncode, 0, f"stdout={r.stdout!r} stderr={r.stderr!r}")

        env = dict(self.env)
        env["VIDUSHI_MAIL_ENDPOINTS"] = json.dumps({
            "gmail:me@emu.test": {"imap_host": "emu.local", "tls_verify": False}})
        r = subprocess.run([sys.executable, STORE, "doctor"],
                           capture_output=True, text=True, env=env)
        payload = json.loads(r.stdout)
        row = payload["accounts"][0]

        self.assertFalse(row["tls_verify"],
                         f"an env-disabled tls_verify must not read as hardened: "
                         f"{row!r}")
        self.assertIn("imap_host=emu.local", row["endpoint"],
                      f"the reported endpoint must be the EFFECTIVE one: {row!r}")
        self.assertIn("tls_verify", row["endpoint_env_override"],
                      f"the row must name the keys the environment supplied — "
                      f"`mail-auth --endpoint` cannot clear those; got {row!r}")

    def test_doctor_tls_remediation_for_an_env_override_names_the_env_var(self):
        """`voa mail-auth --endpoint` cannot clear an env-supplied `tls_verify:
        false` — the environment layer wins over whatever is stored — so advising it
        would send the user round a loop that never restores verification."""
        r = self._mail_auth(
            "--provider", "gmail", "--address", "me@emu.test",
            "--secret-ref", "vidushi-oa/gmail:me@emu.test", "--send")
        self.assertEqual(r.returncode, 0, f"stdout={r.stdout!r} stderr={r.stderr!r}")

        env = dict(self.env)
        env["VIDUSHI_MAIL_ENDPOINTS"] = json.dumps({
            "gmail:me@emu.test": {"tls_verify": False}})
        r = subprocess.run([sys.executable, STORE, "doctor"],
                           capture_output=True, text=True, env=env)
        payload = json.loads(r.stdout)
        steps = [s for s in payload["remediation"] if "tls_verify" in s["step"]]
        self.assertEqual(len(steps), 1, f"expected one TLS step: {payload!r}")
        step = steps[0]["step"]
        self.assertIn("VIDUSHI_MAIL_ENDPOINTS", step,
                      f"the step must name the variable that disabled verification; "
                      f"got {step!r}")
        self.assertNotIn("voa mail-auth --provider", step,
                         f"the step must not advise a command that cannot clear an "
                         f"env-level override; got {step!r}")
        self.assertTrue(steps[0]["human_input"],
                        f"changing the environment is not unattended: {steps[0]!r}")

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


# ---------------------------------------------------------------------------
# Item 8 — the override survives a re-registration, reaches XOAUTH2 SMTP, and a
# malformed env var degrades to "no override" instead of taking every verb down
# ---------------------------------------------------------------------------

def test_re_registering_an_account_without_an_endpoint_preserves_the_configured_one(
        tmp_path):
    config_path = tmp_path / "accounts.json"
    endpoint = {"imap_host": "emu.local", "imap_port": 1143}
    accounts.add_account("gmail:me@emu.test", "gmail", "me@emu.test", "ref-1",
                         endpoint=endpoint, path=str(config_path))

    # A secret rotation / `doctor --fix` re-provision passes no endpoint at all.
    rotated = accounts.add_account("gmail:me@emu.test", "gmail", "me@emu.test",
                                   "ref-2", path=str(config_path))

    assert rotated["secret_ref"] == "ref-2"
    assert rotated["endpoint"] == endpoint, (
        "a re-registration that omits --endpoint must not drop the configured "
        f"override; got {rotated!r}")
    assert json.loads(config_path.read_text())[0]["endpoint"] == endpoint


def test_gmail_xoauth2_adapter_honours_the_endpoint_imap_and_smtp_override():
    creds = json.dumps({"client_id": "cid", "client_secret": "cs",
                        "refresh_token": "rt"})
    adapter = _default_adapter_factory(
        provider="gmail", account="g_xoauth2_emu", address="me@emu.test",
        secret_ref="r1", resolver=_FixedResolver(creds),
        auth_mode="xoauth2",
        endpoint={"imap_host": "emu.local", "imap_port": 1143,
                  "smtp_host": "emu.local", "smtp_port": 1587},
    )

    assert (adapter.host, adapter.port) == ("emu.local", 1143)
    assert (adapter.smtp_host, adapter.smtp_port) == ("emu.local", 1587), (
        "the XOAUTH2 send path must dial the endpoint override's submission "
        f"host/port; got {adapter.smtp_host}:{adapter.smtp_port}")


def test_gmail_xoauth2_adapter_keeps_real_defaults_when_endpoint_absent():
    creds = json.dumps({"client_id": "cid", "client_secret": "cs",
                        "refresh_token": "rt"})
    adapter = _default_adapter_factory(
        provider="gmail", account="g_xoauth2_real", address="me@gmail.com",
        secret_ref="r1", resolver=_FixedResolver(creds),
        auth_mode="xoauth2", endpoint=None)

    assert (adapter.host, adapter.port) == ("imap.gmail.com", 993)
    assert (adapter.smtp_host, adapter.smtp_port) == ("smtp.gmail.com", 587)


@pytest.mark.parametrize("raw", ['{"a": ', "[]", '"nope"', "17"])
def test_malformed_endpoints_env_var_is_ignored_with_a_warning_not_a_traceback(
        tmp_path, monkeypatch, capsys, raw):
    config_path = tmp_path / "accounts.json"
    accounts.add_account("fastmail_real", "fastmail", "me@fastmail.com", "ref-f",
                         path=str(config_path))
    monkeypatch.setenv("VIDUSHI_MAIL_ENDPOINTS", raw)

    client = build_client(config_path=str(config_path), resolver=FakeResolver())

    assert client.build_failures == [], (
        "a malformed VIDUSHI_MAIL_ENDPOINTS must not fail every account; "
        f"got {client.build_failures!r}")
    assert client._adapters["fastmail_real"].session_url == FASTMAIL_DEFAULT_SESSION_URL
    assert "VIDUSHI_MAIL_ENDPOINTS" in capsys.readouterr().err, (
        "the ignored override must be reported on stderr, naming the variable")


def test_endpoints_env_var_with_a_non_object_account_value_is_ignored(
        tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "accounts.json"
    accounts.add_account("fastmail_real", "fastmail", "me@fastmail.com", "ref-f",
                         path=str(config_path))
    monkeypatch.setenv("VIDUSHI_MAIL_ENDPOINTS", json.dumps({"fastmail_real": "emu"}))

    client = build_client(config_path=str(config_path), resolver=FakeResolver())

    assert client.build_failures == []
    assert client._adapters["fastmail_real"].session_url == FASTMAIL_DEFAULT_SESSION_URL
    assert "VIDUSHI_MAIL_ENDPOINTS" in capsys.readouterr().err


if __name__ == "__main__":
    unittest.main()
