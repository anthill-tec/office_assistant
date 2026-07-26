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
import pytest

from vidushi_oa.mail.factory import _default_adapter_factory, build_client
from vidushi_oa.mail.imap import GmailImapAdapter, YahooImapAdapter
from vidushi_oa.mail.jmap import JmapAdapter


class FakeResolver:
    """Deterministic stand-in for `SecretResolver` — no vault/keyring/file I/O."""

    def resolve(self, ref):
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
