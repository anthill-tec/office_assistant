"""Assemble a `MailClient` from the configured accounts (CR-OA-020 §S5).

`build_client` reads the reference-only account registry, constructs one adapter
per account (via an injectable `adapter_factory` — the test seam), stamps the
provider's canonical `source_tag`, and registers each under its account name.

Secret resolution is LAZY: `build_client` itself never calls `resolver.resolve`.
The default `adapter_factory` resolves the secret only when it actually builds a
concrete adapter (and even then, per-account, when `build_client` runs).
"""
import json
import os

from vidushi_oa.mail.accounts import load_accounts
from vidushi_oa.mail.base import SOURCE_TAGS
from vidushi_oa.mail.client import MailClient

# Process-level endpoint override: a JSON object keyed by account NAME, mapping each
# to an endpoint override (`jmap_url` / `imap_host` / `imap_port` / `smtp_host` /
# `smtp_port`). Consulted ONLY when set — unset means every account keeps its own
# persisted endpoint (usually none), so a real deployment is untouched.
_ENDPOINTS_ENV = "VIDUSHI_MAIL_ENDPOINTS"


def _env_endpoints() -> dict:
    """Parse the `VIDUSHI_MAIL_ENDPOINTS` override map, or `{}` when unset/blank."""
    raw = os.environ.get(_ENDPOINTS_ENV)
    if not raw:
        return {}
    return json.loads(raw)


def _imap_endpoint_kwargs(endpoint, default_host):
    """Resolve the IMAP/SMTP adapter kwargs from an optional `endpoint` override.

    Returns `(host, kwargs)` where `host` is `endpoint.imap_host` (else
    `default_host`) and `kwargs` carries `port`/`smtp_host`/`smtp_port` only when the
    override supplies them — so an absent override yields the real provider defaults
    (IMAP :993 and the host-derived SMTP submission host on :587)."""
    endpoint = endpoint or {}
    host = endpoint.get("imap_host") or default_host
    kwargs = {}
    if endpoint.get("imap_port"):
        kwargs["port"] = endpoint["imap_port"]
    if endpoint.get("smtp_host"):
        kwargs["smtp_host"] = endpoint["smtp_host"]
    if endpoint.get("smtp_port"):
        kwargs["smtp_port"] = endpoint["smtp_port"]
    return host, kwargs


def _default_adapter_factory(provider, account, address, secret_ref, resolver,
                             auth_mode="password", transport=None, endpoint=None,
                             conn_factory=None):
    """Real adapter construction (not exercised by the §S5 test suite).

    Resolves the secret reference through `resolver` and builds the concrete
    provider adapter. For `gmail` with `auth_mode == "xoauth2"` the resolved
    secret is a JSON blob `{client_id, client_secret, refresh_token}`; the token
    mint (via `refresh_access_token`, `transport` defaulting to the stdlib urllib
    transport) is deferred into a lazy token provider so no network runs here —
    the `GmailXoauth2Adapter` refreshes on its first connect, not at build time.

    An optional `endpoint` mapping (any of `jmap_url` / `imap_host` / `imap_port` /
    `smtp_host` / `smtp_port`) points the built adapter at a local emulator, and an
    injectable `conn_factory` seams the IMAP socket; both default to the real
    provider so a bare install is byte-for-byte unchanged.
    """
    from vidushi_oa.mail.imap import GmailImapAdapter, YahooImapAdapter
    from vidushi_oa.mail.jmap import fastmail_adapter
    from vidushi_oa.mail.secrets import SecretResolver

    resolver = resolver or SecretResolver()
    secret = resolver.resolve(secret_ref)
    if provider == "gmail":
        host, imap_kwargs = _imap_endpoint_kwargs(endpoint, "imap.gmail.com")
        if auth_mode == "xoauth2":
            from vidushi_oa.mail.xoauth2 import (GmailXoauth2Adapter,
                                                 refresh_access_token)
            creds = json.loads(secret)

            def _token_provider():
                return refresh_access_token(
                    creds["client_id"], creds["client_secret"],
                    creds["refresh_token"], transport=transport,
                )

            port = imap_kwargs.get("port", 993)
            return GmailXoauth2Adapter(account, SOURCE_TAGS["gmail"], host, address,
                                       _token_provider, port=port,
                                       conn_factory=conn_factory)
        return GmailImapAdapter(account, SOURCE_TAGS["gmail"], host, address, secret,
                                conn_factory=conn_factory, **imap_kwargs)
    if provider == "yahoo":
        host, imap_kwargs = _imap_endpoint_kwargs(endpoint, "imap.mail.yahoo.com")
        return YahooImapAdapter(account, SOURCE_TAGS["yahoo"], host, address, secret,
                                conn_factory=conn_factory, **imap_kwargs)
    if provider == "fastmail":
        # Fastmail's primary path is JMAP (DN-mail-access): hand fastmail_adapter
        # the JMAP config so it builds a JmapAdapter with .token == secret.
        # TODO: per-account Basic-plan IMAP fallback (app-password) needs a future
        # account auth-mode field to select the IMAP path instead of JMAP.
        return fastmail_adapter(
            account, SOURCE_TAGS["fastmail"],
            {"jmap_token": secret, "username": address, "endpoint": endpoint or {}},
            transport=transport, conn_factory=conn_factory)
    raise ValueError(f"unsupported provider: {provider!r}")


def build_client(config_path=None, resolver=None, adapter_factory=None) -> MailClient:
    """Build a `MailClient` wired from the configured accounts.

    For each account the `adapter_factory` is called by keyword with
    `provider`/`account`/`address`/`secret_ref`/`resolver`/`auth_mode`; the returned
    adapter's `.source_tag` is stamped from the provider and it is registered under
    the account name.

    Fail-soft, per-account: building one account (the default factory resolves its
    `secret_ref` here) may fail — an unresolvable/rotated secret or a locked
    keyring raises inside the factory. Such an account is recorded
    in `client.build_failures` (name + short reason, never the secret) and SKIPPED,
    so the healthy accounts still register and one stale secret can't blank the
    whole `mail-search`/`mail-accounts`/`mail-get` fan-out.
    """
    factory = adapter_factory or _default_adapter_factory
    env_endpoints = _env_endpoints()
    client = MailClient()
    failures: list[dict] = []
    for entry in load_accounts(config_path):
        name = entry["name"]
        try:
            provider = entry["provider"]
            # The persisted per-account endpoint, then the process-level
            # VIDUSHI_MAIL_ENDPOINTS override (when set) layered on top by key.
            endpoint = dict(entry.get("endpoint") or {})
            endpoint.update(env_endpoints.get(name) or {})
            kwargs = dict(
                provider=provider,
                account=name,
                address=entry["address"],
                secret_ref=entry["secret_ref"],
                resolver=resolver,
                auth_mode=entry.get("auth_mode", "password"),
            )
            # Only forward `endpoint` when there is one — an injected test factory
            # need not accept the kwarg, and a bare account stays byte-for-byte as before.
            if endpoint:
                kwargs["endpoint"] = endpoint
            adapter = factory(**kwargs)
            adapter.source_tag = SOURCE_TAGS.get(provider, adapter.source_tag)
        except Exception as exc:
            failures.append({"account": name, "error": str(exc) or exc.__class__.__name__})
            continue
        client.register(name, adapter)
    client.build_failures = failures
    return client
