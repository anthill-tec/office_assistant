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

    def register(self, name: str, adapter: MailAdapter) -> None:
        """Register (or replace) an adapter under `name`."""
        self._adapters[name] = adapter

    def accounts(self) -> list[tuple[str, set]]:
        """Return `(account_name, capabilities)` for each registered adapter."""
        return [(name, adapter.capabilities()) for name, adapter in self._adapters.items()]

    def search(self, query, accounts=None) -> list[Message]:
        """Dispatch `query` to the selected accounts (all if `accounts is None`),
        merge the results, and de-dup by `Message.id`."""
        selected = self._adapters.keys() if accounts is None else accounts

        merged: list[Message] = []
        seen_ids: set = set()
        for name in selected:
            adapter = self._adapters.get(name)
            if adapter is None:
                continue
            for message in adapter.search(query):
                if message.id:
                    if message.id in seen_ids:
                        continue
                    seen_ids.add(message.id)
                merged.append(message)
        return merged
