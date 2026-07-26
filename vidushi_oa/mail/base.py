"""Provider-agnostic mail primitives (CR-OA-020 §S1).

`Message` is the common shape every provider adapter normalizes its results into;
`MailAdapter` is the abstract base each concrete provider (JMAP/IMAP/…) implements.
The `MailClient` in `client.py` dispatches across registered adapters, merges their
`Message` lists, and de-dups by `Message.id` (the RFC `Message-ID`).
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class Message:
    """A single mail message, normalized across providers.

    `id` is the RFC `Message-ID` and serves as the cross-account dedup key; an
    empty/falsy `id` is never treated as a duplicate of another. All fields are
    optional so adapters can populate only what a given provider exposes.
    """

    id: str = ""
    account: str = ""
    source_tag: str = ""
    subject: str = ""
    sender: str = ""
    to: str = ""
    date: str = ""
    snippet: str = ""
    thread_id: str | None = None
    uid: str | None = None
    folder: str | None = None


class MailAdapter(ABC):
    """Abstract base every provider adapter implements.

    Concrete instances carry an `account` name and a `source_tag` (a short label
    such as ``[GM]`` stamped onto rows so merged results stay traceable to their
    origin). A subclass must implement all four operations below to be instantiable.
    """

    account: str
    source_tag: str

    @abstractmethod
    def capabilities(self) -> set:
        """Return the set of capability flags this adapter supports."""

    @abstractmethod
    def search(self, query, folder=None, limit=None) -> list:
        """Run `query` against the account and return a list of `Message`."""

    @abstractmethod
    def fetch_message(self, uid, folder=None):
        """Fetch a single `Message` by its provider `uid`."""

    @abstractmethod
    def list_folders(self) -> list:
        """Return the list of folder names available on the account."""
