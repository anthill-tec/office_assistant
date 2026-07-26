"""Unified `MailClient` — dispatch/merge/dedup across mail adapters (CR-OA-020 §S1).

A `MailClient` holds a `{account_name: MailAdapter}` registry, dispatches a search
to the selected accounts, concatenates the per-adapter `Message` lists, and de-dups
by `Message.id` (the RFC `Message-ID`): duplicate non-empty ids collapse to a single
row (first occurrence wins); rows with a falsy/empty id are all kept.
"""
from vidushi_oa.mail.base import MailAdapter, Message


class MailClient:
    """Fan-out mail search across registered provider adapters."""

    def __init__(self, adapters: dict[str, MailAdapter] | None = None):
        self._adapters: dict[str, MailAdapter] = dict(adapters or {})
        self.last_failures: list[dict] = []
        self.last_succeeded: int = 0
        self.build_failures: list[dict] = []

    def register(self, name: str, adapter: MailAdapter) -> None:
        """Register (or replace) an adapter under `name`."""
        self._adapters[name] = adapter

    def accounts(self) -> list[tuple[str, set]]:
        """Return `(account_name, capabilities)` for each registered adapter."""
        return [(name, adapter.capabilities()) for name, adapter in self._adapters.items()]

    def search(self, query, accounts=None) -> list[Message]:
        """Dispatch `query` to the selected accounts (all if `accounts is None`),
        merge the results, and de-dup by `Message.id`.

        Fail-soft with per-adapter error isolation: one account raising (a revoked
        XOAUTH2 token -> `LookupError`, or a down IMAP host) does NOT abort the
        fan-out — that account is recorded in `last_failures` (name + short reason,
        never the secret) and the healthy accounts' results are still returned.
        Accounts that could not even be built (`build_failures` — e.g. an
        unresolvable `secret_ref`) fold into the same `last_failures` for the
        selected accounts, so a stale secret on one account is surfaced rather than
        blanking the whole search. `last_succeeded` counts the adapters that ran
        clean, so the caller can tell a partial failure (some succeeded) from a
        total wipeout (none did)."""
        selected = list(self._adapters.keys()) if accounts is None else list(accounts)
        build_fail_map = {f["account"]: f for f in self.build_failures}

        merged: list[Message] = []
        seen_ids: set = set()
        failures: list[dict] = []
        succeeded = 0
        for name in selected:
            adapter = self._adapters.get(name)
            if adapter is None:
                build_failure = build_fail_map.get(name)
                if build_failure is not None:
                    failures.append(build_failure)
                continue
            try:
                messages = list(adapter.search(query))
            except Exception as exc:
                failures.append({"account": name, "error": str(exc) or exc.__class__.__name__})
                continue
            succeeded += 1
            for message in messages:
                if message.id:
                    if message.id in seen_ids:
                        continue
                    seen_ids.add(message.id)
                merged.append(message)
        if accounts is None:
            for name, build_failure in build_fail_map.items():
                if name not in self._adapters:
                    failures.append(build_failure)
        self.last_failures = failures
        self.last_succeeded = succeeded
        return merged
