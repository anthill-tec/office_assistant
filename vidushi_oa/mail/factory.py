"""Assemble a `MailClient` from the configured accounts (CR-OA-020 §S5).

`build_client` reads the reference-only account registry, constructs one adapter
per account (via an injectable `adapter_factory` — the test seam), stamps the
provider's canonical `source_tag`, and registers each under its account name.

Secret resolution is LAZY: `build_client` itself never calls `resolver.resolve`.
The default `adapter_factory` resolves the secret only when it actually builds a
concrete adapter (and even then, per-account, when `build_client` runs).
"""
from vidushi_oa.mail.accounts import load_accounts
from vidushi_oa.mail.client import MailClient

_SOURCE_TAGS = {"gmail": "[GM]", "yahoo": "[YH]", "fastmail": "[FM]"}


def _default_adapter_factory(provider, account, address, secret_ref, resolver):
    """Real adapter construction (not exercised by the §S5 test suite).

    Resolves the secret reference through `resolver` and builds the concrete
    provider adapter.
    """
    from vidushi_oa.mail.imap import GmailImapAdapter, YahooImapAdapter
    from vidushi_oa.mail.jmap import fastmail_adapter
    from vidushi_oa.mail.secrets import SecretResolver

    resolver = resolver or SecretResolver()
    secret = resolver.resolve(secret_ref)
    if provider == "gmail":
        return GmailImapAdapter(account, _SOURCE_TAGS["gmail"],
                                "imap.gmail.com", address, secret)
    if provider == "yahoo":
        return YahooImapAdapter(account, _SOURCE_TAGS["yahoo"],
                                "imap.mail.yahoo.com", address, secret)
    if provider == "fastmail":
        return fastmail_adapter(account, _SOURCE_TAGS["fastmail"],
                                {"address": address, "token": secret})
    raise ValueError(f"unsupported provider: {provider!r}")


def build_client(config_path=None, resolver=None, adapter_factory=None) -> MailClient:
    """Build a `MailClient` wired from the configured accounts.

    For each account the `adapter_factory` is called by keyword with
    `provider`/`account`/`address`/`secret_ref`/`resolver`; the returned adapter's
    `.source_tag` is stamped from the provider and it is registered under the
    account name. No secret is resolved here.
    """
    factory = adapter_factory or _default_adapter_factory
    client = MailClient()
    for entry in load_accounts(config_path):
        provider = entry["provider"]
        adapter = factory(
            provider=provider,
            account=entry["name"],
            address=entry["address"],
            secret_ref=entry["secret_ref"],
            resolver=resolver,
        )
        adapter.source_tag = _SOURCE_TAGS.get(provider, adapter.source_tag)
        client.register(entry["name"], adapter)
    return client
