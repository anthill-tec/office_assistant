"""CR-OA-020 §S1 — unified `MailClient` interface + capability flags (RED).

The mail subsystem does not exist yet: `vidushi_oa.mail` (and its `base`/`client` modules)
are not on disk, so every import below fails with `ModuleNotFoundError` until CR-OA-020's
GREEN phase lands:

  - `vidushi_oa/mail/base.py` — the provider-agnostic `Message` dataclass (the common
    message shape every adapter normalizes into) and the abstract `MailAdapter` base
    (`capabilities()`, `search()`, `fetch_message()`, `list_folders()`, plus `account`/
    `source_tag` attributes).
  - `vidushi_oa/mail/client.py` — `MailClient`, constructed from a `{account_name: adapter}`
    mapping (or built via `register()`), that dispatches `search()` across the selected
    accounts, merges the results, and de-dups by `Message.id` (the RFC `Message-ID`, per
    §S1 AC: "a registered fake adapter is dispatched by account; `mail-search` across two
    fake accounts returns a single merged result set" — dedup itself is the §S5 AC's
    "two accounts returning the same `Message-ID` de-dup to one row", exercised here at
    the `MailClient` layer since §S1 is where the merge/dedup mechanism lives), and
    `accounts()` surfacing each registered account's name + capability set.

No real IMAP/JMAP/network — a small in-file `FakeAdapter(MailAdapter)` returns canned
`Message` lists and canned capabilities, matching the "registered fake adapter" language
in the §S1 AC.
"""
import unittest
from abc import ABC

from vidushi_oa.mail.base import MailAdapter, Message
from vidushi_oa.mail.client import MailClient


def _msg(id_, account, source_tag, subject="Subject", uid="1"):
    return Message(
        id=id_,
        account=account,
        source_tag=source_tag,
        subject=subject,
        sender="sender@example.com",
        to="me@example.com",
        date="2026-07-20T10:00:00Z",
        snippet="a snippet",
        thread_id="thread-1",
        uid=uid,
        folder="INBOX",
    )


class FakeAdapter(MailAdapter):
    """Canned adapter — no network. Returns a fixed list of Messages regardless of query,
    and declares a fixed capability set, so tests exercise MailClient dispatch/merge/dedup
    without touching any real protocol."""

    def __init__(self, account, source_tag, caps, messages=None):
        self.account = account
        self.source_tag = source_tag
        self._caps = set(caps)
        self._messages = messages if messages is not None else []
        self.search_calls = []

    def capabilities(self):
        return set(self._caps)

    def search(self, query, folder=None, limit=None):
        self.search_calls.append((query, folder, limit))
        results = list(self._messages)
        if limit is not None:
            results = results[:limit]
        return results

    def fetch_message(self, uid, folder=None):
        for m in self._messages:
            if m.uid == uid:
                return m
        raise KeyError(uid)

    def list_folders(self):
        return ["INBOX"]


class MailAdapterAbstractnessTest(unittest.TestCase):
    """§S1 AC groundwork: `MailAdapter` is the abstract base every provider adapter
    implements — it must not be directly instantiable, but a concrete subclass
    implementing all four operations must be."""

    def test_mail_adapter_is_abstract_base_class(self):
        self.assertTrue(issubclass(MailAdapter, ABC))

    def test_mail_adapter_cannot_be_instantiated_directly(self):
        with self.assertRaises(TypeError):
            MailAdapter()

    def test_concrete_subclass_implementing_all_four_methods_is_instantiable(self):
        adapter = FakeAdapter("gmail_main", "[GM]", {"raw_query"})
        self.assertIsInstance(adapter, MailAdapter)


class MailClientAccountsTest(unittest.TestCase):
    """§S1 AC: "a registered fake adapter is dispatched by account" — `accounts()` must
    surface each registered account's name and capability set so callers (and the
    §S5 `mail-accounts` verb) can report what's configured."""

    def test_accounts_lists_each_registered_account_with_its_capabilities(self):
        gmail = FakeAdapter("gmail_main", "[GM]", {"raw_query", "server_threads"})
        fastmail = FakeAdapter("fastmail_main", "[FM]", {"server_side_categories"})
        client = MailClient({"gmail_main": gmail, "fastmail_main": fastmail})

        result = client.accounts()

        self.assertEqual(
            sorted(result),
            [
                ("fastmail_main", {"server_side_categories"}),
                ("gmail_main", {"raw_query", "server_threads"}),
            ],
        )

    def test_accounts_empty_when_no_adapters_registered(self):
        client = MailClient({})
        self.assertEqual(client.accounts(), [])

    def test_register_method_adds_an_account_reflected_in_accounts(self):
        client = MailClient({})
        adapter = FakeAdapter("yahoo_main", "[YH]", {"legacy_only"})
        client.register("yahoo_main", adapter)

        self.assertEqual(client.accounts(), [("yahoo_main", {"legacy_only"})])


class MailClientSearchMergeTest(unittest.TestCase):
    """§S1 AC: "`mail-search` across two fake accounts returns a single merged result
    set" — MailClient.search() must dispatch to every selected account's adapter,
    concatenate the results, and preserve each row's originating source_tag."""

    def setUp(self):
        self.gmail = FakeAdapter(
            "gmail_main", "[GM]", {"raw_query"},
            messages=[_msg("<msg-gm-1@gmail.com>", "gmail_main", "[GM]", uid="gm-1")],
        )
        self.fastmail = FakeAdapter(
            "fastmail_main", "[FM]", {"server_side_categories"},
            messages=[_msg("<msg-fm-1@fastmail.com>", "fastmail_main", "[FM]", uid="fm-1")],
        )
        self.client = MailClient({"gmail_main": self.gmail, "fastmail_main": self.fastmail})

    def test_search_merges_results_from_all_registered_accounts(self):
        results = self.client.search("invoice")

        ids = sorted(m.id for m in results)
        self.assertEqual(ids, ["<msg-fm-1@fastmail.com>", "<msg-gm-1@gmail.com>"])

    def test_search_result_rows_keep_their_originating_source_tag(self):
        results = self.client.search("invoice")

        tags_by_id = {m.id: m.source_tag for m in results}
        self.assertEqual(tags_by_id["<msg-gm-1@gmail.com>"], "[GM]")
        self.assertEqual(tags_by_id["<msg-fm-1@fastmail.com>"], "[FM]")

    def test_search_dispatches_the_query_string_to_every_adapter(self):
        self.client.search("invoice")

        self.assertEqual(self.gmail.search_calls, [("invoice", None, None)])
        self.assertEqual(self.fastmail.search_calls, [("invoice", None, None)])

    def test_search_with_accounts_filter_dispatches_only_to_selected_account(self):
        results = self.client.search("invoice", accounts=["gmail_main"])

        self.assertEqual([m.id for m in results], ["<msg-gm-1@gmail.com>"])
        self.assertEqual(self.gmail.search_calls, [("invoice", None, None)])
        self.assertEqual(self.fastmail.search_calls, [])


class MailClientDedupTest(unittest.TestCase):
    """§S1/§S5 AC: "two accounts returning the same `Message-ID` de-dup to one row" —
    MailClient.search() must collapse duplicate ids across accounts to exactly one row,
    keep distinct ids, and NOT collapse messages with an empty/falsy id."""

    def test_same_message_id_from_two_accounts_collapses_to_one_row(self):
        shared_id = "<shared@example.com>"
        gmail = FakeAdapter(
            "gmail_main", "[GM]", {"raw_query"},
            messages=[_msg(shared_id, "gmail_main", "[GM]", uid="gm-1")],
        )
        fastmail = FakeAdapter(
            "fastmail_main", "[FM]", {"server_side_categories"},
            messages=[_msg(shared_id, "fastmail_main", "[FM]", uid="fm-1")],
        )
        client = MailClient({"gmail_main": gmail, "fastmail_main": fastmail})

        results = client.search("invoice")

        matching = [m for m in results if m.id == shared_id]
        self.assertEqual(len(matching), 1)

    def test_distinct_message_ids_are_all_kept(self):
        gmail = FakeAdapter(
            "gmail_main", "[GM]", {"raw_query"},
            messages=[
                _msg("<a@example.com>", "gmail_main", "[GM]", uid="gm-1"),
                _msg("<b@example.com>", "gmail_main", "[GM]", uid="gm-2"),
            ],
        )
        fastmail = FakeAdapter(
            "fastmail_main", "[FM]", {"server_side_categories"},
            messages=[_msg("<c@example.com>", "fastmail_main", "[FM]", uid="fm-1")],
        )
        client = MailClient({"gmail_main": gmail, "fastmail_main": fastmail})

        results = client.search("invoice")

        self.assertEqual(
            sorted(m.id for m in results),
            ["<a@example.com>", "<b@example.com>", "<c@example.com>"],
        )

    def test_empty_id_messages_are_not_collapsed_together(self):
        gmail = FakeAdapter(
            "gmail_main", "[GM]", {"raw_query"},
            messages=[_msg("", "gmail_main", "[GM]", uid="gm-1", subject="First")],
        )
        fastmail = FakeAdapter(
            "fastmail_main", "[FM]", {"server_side_categories"},
            messages=[_msg("", "fastmail_main", "[FM]", uid="fm-1", subject="Second")],
        )
        client = MailClient({"gmail_main": gmail, "fastmail_main": fastmail})

        results = client.search("invoice")

        empty_id_rows = [m for m in results if not m.id]
        self.assertEqual(len(empty_id_rows), 2)
        self.assertEqual(
            sorted(m.subject for m in empty_id_rows), ["First", "Second"]
        )


class RaisingAdapter(FakeAdapter):
    """A FakeAdapter whose `search` always raises, to exercise `MailClient`'s
    per-adapter error isolation (a revoked token / down host)."""

    def __init__(self, account, source_tag, caps, error):
        super().__init__(account, source_tag, caps)
        self._error = error

    def search(self, query, folder=None, limit=None):
        raise self._error


class MailClientSearchIsolationTest(unittest.TestCase):
    """One failing adapter must not abort the fan-out: the healthy account's rows are
    returned, and the failure is recorded on `last_failures` (name + reason, never a
    secret) with `last_succeeded` distinguishing partial from total failure."""

    def test_one_failing_adapter_does_not_blank_the_healthy_account(self):
        gmail = FakeAdapter(
            "gmail_main", "[GM]", {"raw_query"},
            messages=[_msg("<gm-1@gmail.com>", "gmail_main", "[GM]", uid="gm-1")],
        )
        yahoo = RaisingAdapter("yahoo_main", "[YH]", {"legacy_only"},
                               LookupError("token refresh failed"))
        client = MailClient({"gmail_main": gmail, "yahoo_main": yahoo})

        results = client.search("invoice")

        self.assertEqual([m.id for m in results], ["<gm-1@gmail.com>"])
        self.assertEqual(client.last_succeeded, 1)
        self.assertEqual([f["account"] for f in client.last_failures], ["yahoo_main"])
        self.assertIn("token refresh failed", client.last_failures[0]["error"])

    def test_all_adapters_failing_yields_no_results_and_zero_successes(self):
        gmail = RaisingAdapter("gmail_main", "[GM]", {"raw_query"}, LookupError("revoked"))
        yahoo = RaisingAdapter("yahoo_main", "[YH]", {"legacy_only"}, OSError("unreachable"))
        client = MailClient({"gmail_main": gmail, "yahoo_main": yahoo})

        results = client.search("invoice")

        self.assertEqual(results, [])
        self.assertEqual(client.last_succeeded, 0)
        self.assertEqual(
            sorted(f["account"] for f in client.last_failures),
            ["gmail_main", "yahoo_main"],
        )


if __name__ == "__main__":
    unittest.main()
