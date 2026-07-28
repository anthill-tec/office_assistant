"""CR-OA-026 §S1 — `_mail_row()` must expose `uid` + `account` (RED).

`_mail_row()` (`vidushi_oa/_cli.py:737-740`) currently projects a `Message` to only
`{id, source_tag, subject, sender, date}`, dropping `msg.uid` and `msg.account` even
though both are already populated on the `Message` dataclass. Without them, a
`mail-search` result row carries no handle `mail-get --account <account> --uid <uid>`
can consume — only the RFC Message-ID, which produces a malformed `UID FETCH`.

Covers (spec AC lines, §S1 only — §S2 AXI-conformance is a separate cycle):
  - `_mail_row(msg)` returns `uid`/`account` alongside the existing fields.
  - A `mail-search` run's rows carry `uid`/`account` in default TOON, `--full`, and
    `--json`.
  - Integration round-trip: a search row's `(account, uid)` is directly consumable by
    `mail-get --account <account> --uid <uid>` against the same fake adapter.

All three fail against current code because `_mail_row` drops `uid`/`account` — no
import/collection error, since every symbol exercised here already exists.
"""
import json
from argparse import Namespace

import pytest

import vidushi_oa._cli as cli
from vidushi_oa.mail.base import MailAdapter, Message
from vidushi_oa.mail.client import MailClient
from vidushi_oa import toon as oa_toon


def _msg(id_, account, source_tag, subject, sender, date, uid):
    return Message(
        id=id_, account=account, source_tag=source_tag, subject=subject,
        sender=sender, to="me@example.com", date=date, uid=uid, folder="INBOX",
    )


class FakeAdapter(MailAdapter):
    """No network — canned `Message`s, matching the CR-OA-020 fake-adapter pattern."""

    def __init__(self, account, source_tag, caps, messages=None):
        self.account = account
        self.source_tag = source_tag
        self._caps = set(caps)
        self._messages = messages if messages is not None else []

    def capabilities(self):
        return set(self._caps)

    def search(self, query, folder=None, limit=None):
        return list(self._messages)

    def fetch_message(self, uid, folder=None):
        for m in self._messages:
            if m.uid == uid:
                return m
        raise KeyError(uid)

    def list_folders(self):
        return ["INBOX"]


ORDER_MSG = _msg(
    "<order-42@example.com>", "fastmail", "[FM]", "Order confirmed",
    "orders@vendor.example", "2026-07-20T10:00:00Z", "42",
)


def _build_fake_client():
    fastmail = FakeAdapter("fastmail", "[FM]", {"server_side_categories"}, messages=[ORDER_MSG])
    return MailClient({"fastmail": fastmail})


@pytest.fixture(autouse=True)
def restore_cli_fmt():
    original = getattr(cli, "_FMT", "toon")
    yield
    cli._FMT = original


def test_mail_row_includes_uid_and_account_alongside_existing_fields():
    """AC: `_mail_row(msg)` returns a dict containing `uid == msg.uid` and
    `account == msg.account`, IN ADDITION TO the existing id/source_tag/subject/
    sender/date — every field asserted against a specific value, not presence-only."""
    row = cli._mail_row(ORDER_MSG)

    assert row["uid"] == "42", f"expected uid '42' from msg.uid, got {row.get('uid')!r}"
    assert row["account"] == "fastmail", f"expected account 'fastmail' from msg.account, got {row.get('account')!r}"
    # Existing projection must be untouched.
    assert row["id"] == "<order-42@example.com>"
    assert row["source_tag"] == "[FM]"
    assert row["subject"] == "Order confirmed"
    assert row["sender"] == "orders@vendor.example"
    assert row["date"] == "2026-07-20T10:00:00Z"


def test_mail_search_default_toon_rows_carry_uid_and_account(monkeypatch, capsys):
    """AC: uid/account are in the MINIMAL DEFAULT projection (no --full, no
    --fields) — not only under --full."""
    client = _build_fake_client()
    monkeypatch.setattr(cli, "build_client", lambda **kw: client)
    cli._FMT = "toon"

    cli.cmd_mail_search(Namespace(query="order", accounts=None, full=False))

    payload = oa_toon.from_toon(capsys.readouterr().out)
    assert payload["count"] == 1
    row = payload["results"][0]
    assert row["uid"] == "42", f"default TOON row must carry uid '42', got {row}"
    assert row["account"] == "fastmail", f"default TOON row must carry account 'fastmail', got {row}"


def test_mail_search_full_mode_rows_carry_uid_and_account(monkeypatch, capsys):
    client = _build_fake_client()
    monkeypatch.setattr(cli, "build_client", lambda **kw: client)
    cli._FMT = "toon"

    cli.cmd_mail_search(Namespace(query="order", accounts=None, full=True))

    payload = oa_toon.from_toon(capsys.readouterr().out)
    row = payload["results"][0]
    assert row["uid"] == "42", f"--full row must carry uid '42', got {row}"
    assert row["account"] == "fastmail", f"--full row must carry account 'fastmail', got {row}"


def test_mail_search_json_mode_rows_carry_uid_and_account(monkeypatch, capsys):
    client = _build_fake_client()
    monkeypatch.setattr(cli, "build_client", lambda **kw: client)
    cli._FMT = "json"

    cli.cmd_mail_search(Namespace(query="order", accounts=None, full=False))

    rows = json.loads(capsys.readouterr().out.strip())
    assert len(rows) == 1
    assert rows[0]["uid"] == "42", f"--json row must carry uid '42', got {rows[0]}"
    assert rows[0]["account"] == "fastmail", f"--json row must carry account 'fastmail', got {rows[0]}"


def test_mail_search_result_uid_and_account_round_trip_into_mail_get(monkeypatch, capsys):
    """Integration (round-trip): the (account, uid) taken from a mail-search row is
    passed straight to `mail-get --account <account> --uid <uid>` against the SAME
    fake adapter, and the message resolves — proving a search result is now directly
    openable. This must fail today because the row lacks `uid`/`account` to carry
    forward (KeyError on the missing keys is the expected RED signal here)."""
    client = _build_fake_client()
    monkeypatch.setattr(cli, "build_client", lambda **kw: client)
    cli._FMT = "json"

    cli.cmd_mail_search(Namespace(query="order", accounts=None, full=False))
    rows = json.loads(capsys.readouterr().out.strip())
    search_row = rows[0]

    # Pull the handle straight from the search row — no hardcoded values from the
    # fixture, proving the row itself carries what mail-get needs.
    uid = search_row["uid"]
    account = search_row["account"]

    cli.cmd_mail_get(Namespace(account=account, uid=uid))
    result = json.loads(capsys.readouterr().out.strip())

    assert result["id"] == ORDER_MSG.id, (
        f"mail-get with the search row's (account={account!r}, uid={uid!r}) must "
        f"resolve the SAME message, got {result}"
    )
    assert result["subject"] == "Order confirmed"
