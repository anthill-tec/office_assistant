"""CR-OA-020 post-VERIFY FIX — real `_default_adapter_factory` + `build_client`
default-path coverage (RED).

VERIFY found a CONFIRMED BLOCKING bug: the production default adapter-factory
(`vidushi_oa.mail.factory._default_adapter_factory`) is never exercised by any
existing CR-OA-020 test — every one of them injects a fake `adapter_factory`
into `build_client`. For a real Fastmail account the default factory calls
``fastmail_adapter(account, source_tag, {"address": address, "token": secret})``,
but `fastmail_adapter` (`vidushi_oa/mail/jmap.py`) looks for `config["jmap_token"]`
(not `"token"`), so it silently falls through to its IMAP fallback path and then
raises `KeyError: 'app_password'` — a crash on every real Fastmail account.

This file drives the REAL default path (no injected `adapter_factory`) end to
end so that regression is caught. No live mail/network: every adapter
constructs lazily (no connection is opened in `__init__`), so this is hermetic.
"""
import json

import pytest

from vidushi_oa.mail.factory import _default_adapter_factory, build_client
from vidushi_oa.mail.imap import GmailImapAdapter, YahooImapAdapter
from vidushi_oa.mail.jmap import JmapAdapter
from vidushi_oa.mail.xoauth2 import GmailXoauth2Adapter


class FakeResolver:
    """Deterministic stand-in for `SecretResolver` — no vault/keyring/file I/O."""

    def resolve(self, ref):
        return f"secret-for-{ref}"


class FakeBlobResolver:
    """Resolver returning the XOAUTH2 JSON credential blob for any ref."""

    def resolve(self, ref):
        return json.dumps({"client_id": "cid", "client_secret": "csecret",
                           "refresh_token": "rtok"})


class FailingRefResolver:
    """Resolves every ref except `bad_ref`, which raises `LookupError` — a rotated/
    deleted vault entry, a missing `op` CLI, or a locked keyring."""

    def __init__(self, bad_ref):
        self._bad_ref = bad_ref

    def resolve(self, ref):
        if ref == self._bad_ref:
            raise LookupError(f"secret_ref {ref!r} could not be resolved")
        return f"secret-for-{ref}"


def test_default_adapter_factory_builds_gmail_imap_adapter_with_resolved_password():
    adapter = _default_adapter_factory(
        provider="gmail", account="g1", address="me@gmail.com",
        secret_ref="r1", resolver=FakeResolver(),
    )

    assert isinstance(adapter, GmailImapAdapter)
    assert adapter.host == "imap.gmail.com"
    assert adapter.user == "me@gmail.com"
    assert adapter.password == "secret-for-r1"


def test_default_adapter_factory_builds_yahoo_imap_adapter_with_resolved_password():
    adapter = _default_adapter_factory(
        provider="yahoo", account="y1", address="me@yahoo.com",
        secret_ref="r2", resolver=FakeResolver(),
    )

    assert isinstance(adapter, YahooImapAdapter)
    assert adapter.host == "imap.mail.yahoo.com"
    assert adapter.user == "me@yahoo.com"
    assert adapter.password == "secret-for-r2"


def test_default_adapter_factory_builds_fastmail_jmap_adapter_without_keyerror():
    """Regression for the VERIFY-confirmed bug: this used to raise
    `KeyError: 'app_password'` for every real Fastmail account because the
    resolved secret was wired under the wrong config key (`"token"` instead of
    the `"jmap_token"` key `fastmail_adapter` actually reads)."""
    adapter = _default_adapter_factory(
        provider="fastmail", account="f1", address="me@fastmail.com",
        secret_ref="r3", resolver=FakeResolver(),
    )

    assert isinstance(adapter, JmapAdapter)
    assert adapter.token == "secret-for-r3"


def test_default_adapter_factory_builds_gmail_xoauth2_adapter_with_lazy_token():
    """Gmail + `auth_mode="xoauth2"` resolves the JSON credential blob and returns
    a `GmailXoauth2Adapter` whose token mint is DEFERRED: the injected transport
    must NOT fire at build time, only on first connect (`_token()`)."""
    calls = {"count": 0}

    def fake_transport(method, url, headers, body):
        calls["count"] += 1
        return 200, {"access_token": "minted-access-token"}

    adapter = _default_adapter_factory(
        provider="gmail", account="gx", address="me@workspace.com",
        secret_ref="rx", resolver=FakeBlobResolver(),
        auth_mode="xoauth2", transport=fake_transport,
    )

    assert isinstance(adapter, GmailXoauth2Adapter)
    assert adapter.host == "imap.gmail.com"
    assert adapter.user == "me@workspace.com"
    assert adapter.source_tag == "[GM]"
    assert calls["count"] == 0, "token refresh must not run at build time"

    assert adapter._token() == "minted-access-token"
    assert calls["count"] == 1
    adapter._token()
    assert calls["count"] == 1, "token provider must be invoked at most once"


def test_default_adapter_factory_gmail_defaults_to_password_when_auth_mode_absent():
    """A gmail account with no `auth_mode` (legacy entry / default) still builds the
    password-based `GmailImapAdapter`, never the XOAUTH2 path."""
    adapter = _default_adapter_factory(
        provider="gmail", account="g1", address="me@gmail.com",
        secret_ref="r1", resolver=FakeResolver(),
    )

    assert isinstance(adapter, GmailImapAdapter)
    assert not isinstance(adapter, GmailXoauth2Adapter)
    assert adapter.password == "secret-for-r1"


def test_build_client_wires_gmail_xoauth2_account_end_to_end(tmp_path, monkeypatch):
    """`build_client` honours a persisted `auth_mode="xoauth2"` gmail account and
    hands the factory the XOAUTH2 path — the registry -> factory -> adapter wiring
    the CLI relies on."""
    from vidushi_oa.mail import accounts

    config_path = tmp_path / "accounts.json"
    monkeypatch.setenv("VIDUSHI_MAIL_CONFIG", str(config_path))
    accounts.add_account("gmail_ws", "gmail", "me@workspace.com", "ref-x",
                         auth_mode="xoauth2")

    calls = {"count": 0}

    def fake_transport(method, url, headers, body):
        calls["count"] += 1
        return 200, {"access_token": "wired-token"}

    def factory(**kw):
        return _default_adapter_factory(transport=fake_transport, **kw)

    client = build_client(config_path=str(config_path), resolver=FakeBlobResolver(),
                          adapter_factory=factory)

    adapter = client._adapters["gmail_ws"]
    assert isinstance(adapter, GmailXoauth2Adapter)
    assert adapter.source_tag == "[GM]"
    assert calls["count"] == 0, "build_client must not trigger a token refresh"
    assert adapter._token() == "wired-token"
    assert calls["count"] == 1


def test_listing_accounts_performs_no_network_for_an_xoauth2_account(tmp_path, monkeypatch):
    """The `mail-accounts` listing (a pure capability enumeration) must do zero
    network for an xoauth2 gmail account: neither `build_client` nor
    `client.accounts()` may fire the token-refresh transport."""
    from vidushi_oa.mail import accounts

    config_path = tmp_path / "accounts.json"
    monkeypatch.setenv("VIDUSHI_MAIL_CONFIG", str(config_path))
    accounts.add_account("gmail_ws", "gmail", "me@workspace.com", "ref-x",
                         auth_mode="xoauth2")

    calls = {"count": 0}

    def fake_transport(method, url, headers, body):
        calls["count"] += 1
        return 200, {"access_token": "wired-token"}

    def factory(**kw):
        return _default_adapter_factory(transport=fake_transport, **kw)

    client = build_client(config_path=str(config_path), resolver=FakeBlobResolver(),
                          adapter_factory=factory)
    listed = dict(client.accounts())

    assert "gmail_ws" in listed
    assert calls["count"] == 0, "mail-accounts must not trigger a token refresh"


def test_default_adapter_factory_rejects_unsupported_provider():
    with pytest.raises(ValueError) as excinfo:
        _default_adapter_factory(
            provider="bogus", account="x1", address="x@example.com",
            secret_ref="r4", resolver=FakeResolver(),
        )
    assert "bogus" in str(excinfo.value)


def test_build_client_default_factory_wires_all_three_providers_without_keyerror(tmp_path, monkeypatch):
    """`build_client` with NO injected `adapter_factory` — the real production
    wiring — across all three providers. Must not raise, and every adapter
    must be a real concrete instance carrying the resolved secret."""
    from vidushi_oa.mail import accounts

    config_path = tmp_path / "accounts.json"
    monkeypatch.setenv("VIDUSHI_MAIL_CONFIG", str(config_path))
    accounts.add_account("gmail_main", "gmail", "me@gmail.com", "ref-g")
    accounts.add_account("yahoo_main", "yahoo", "me@yahoo.com", "ref-y")
    accounts.add_account("fastmail_main", "fastmail", "me@fastmail.com", "ref-f")

    client = build_client(config_path=str(config_path), resolver=FakeResolver())

    registered = dict(client.accounts())
    assert set(registered.keys()) == {"gmail_main", "yahoo_main", "fastmail_main"}

    assert isinstance(client._adapters["gmail_main"], GmailImapAdapter)
    assert isinstance(client._adapters["yahoo_main"], YahooImapAdapter)
    assert isinstance(client._adapters["fastmail_main"], JmapAdapter)

    assert client._adapters["gmail_main"].password == "secret-for-ref-g"
    assert client._adapters["yahoo_main"].password == "secret-for-ref-y"
    assert client._adapters["fastmail_main"].token == "secret-for-ref-f"

    assert client._adapters["gmail_main"].source_tag == "[GM]"
    assert client._adapters["yahoo_main"].source_tag == "[YH]"
    assert client._adapters["fastmail_main"].source_tag == "[FM]"


def test_add_account_upserts_by_name_on_secret_rotation(tmp_path, monkeypatch):
    """Re-running `voa mail-auth` for an existing account name (secret rotation)
    replaces the entry in place — exactly one row, carrying the latest
    `secret_ref`, so `voa doctor` never shows a duplicate/stale account."""
    from vidushi_oa.mail import accounts

    config_path = tmp_path / "accounts.json"
    monkeypatch.setenv("VIDUSHI_MAIL_CONFIG", str(config_path))

    accounts.add_account("gmail_main", "gmail", "me@gmail.com", "ref-old")
    accounts.add_account("yahoo_main", "yahoo", "me@yahoo.com", "ref-y")
    accounts.add_account("gmail_main", "gmail", "me@gmail.com", "ref-new")

    rows = accounts.load_accounts(str(config_path))
    assert [r["name"] for r in rows] == ["gmail_main", "yahoo_main"]
    gmail = next(r for r in rows if r["name"] == "gmail_main")
    assert gmail["secret_ref"] == "ref-new"


def test_build_client_isolates_an_account_whose_secret_cannot_resolve(tmp_path, monkeypatch):
    """One account's unresolvable `secret_ref` must NOT abort building the others:
    the healthy account still registers, and the failure is recorded in
    `build_failures` (name + reason, never the resolved secret)."""
    from vidushi_oa.mail import accounts

    config_path = tmp_path / "accounts.json"
    monkeypatch.setenv("VIDUSHI_MAIL_CONFIG", str(config_path))
    accounts.add_account("gmail_main", "gmail", "me@gmail.com", "good-ref")
    accounts.add_account("yahoo_main", "yahoo", "me@yahoo.com", "bad-ref")

    client = build_client(config_path=str(config_path),
                          resolver=FailingRefResolver("bad-ref"))

    registered = dict(client.accounts())
    assert set(registered.keys()) == {"gmail_main"}
    assert isinstance(client._adapters["gmail_main"], GmailImapAdapter)
    assert client._adapters["gmail_main"].password == "secret-for-good-ref"
    assert [f["account"] for f in client.build_failures] == ["yahoo_main"]
    assert "bad-ref" in client.build_failures[0]["error"]
    assert "secret-for" not in client.build_failures[0]["error"]


def test_build_client_build_failure_folds_into_search_fail_soft_reporting(tmp_path, monkeypatch):
    """A build-time failure (unresolvable secret) folds into `MailClient.search`'s
    fail-soft reporting for the selected account — no network, `last_succeeded == 0`,
    and the failed account surfaced in `last_failures`."""
    from vidushi_oa.mail import accounts

    config_path = tmp_path / "accounts.json"
    monkeypatch.setenv("VIDUSHI_MAIL_CONFIG", str(config_path))
    accounts.add_account("gmail_main", "gmail", "me@gmail.com", "good-ref")
    accounts.add_account("yahoo_main", "yahoo", "me@yahoo.com", "bad-ref")

    client = build_client(config_path=str(config_path),
                          resolver=FailingRefResolver("bad-ref"))

    results = client.search("invoice", accounts=["yahoo_main"])

    assert results == []
    assert client.last_succeeded == 0
    assert [f["account"] for f in client.last_failures] == ["yahoo_main"]
