"""CR-OA-026 §S2 — `mail-search` AXI-conformant `next[]` hint + empty state (RED).

§S1 (a separate, already-GREEN cycle) made `_mail_row()` carry `uid`/`account`. This
cycle covers §S2: the response must be AXI-conformant, not merely carry the fields.

Covers (spec AC lines, §S2 only):
  - AXI #2 (minimal default fields): `uid`/`account` appear in the DEFAULT TOON
    projection (no `--full`, no `--fields`) — included here as a guard, not the real
    RED (it already passes off the §S1 GREEN).
  - AXI #9 (contextual next-command hint): `mail-search`'s `next[]` contains a
    RUNNABLE `mail-get --account <account> --uid <uid>` string built from the FIRST
    result row's ACTUAL `account`/`uid` values — asserted as the exact interpolated
    command, not a template/placeholder. This is the genuine RED: current
    `cmd_mail_search` (`vidushi_oa/_cli.py:783`) builds
    `nxt = [f"mail-search {a.query} --accounts <name>", "mail-accounts"]` — no
    `mail-get` hint at all.
  - AXI #1/#5 (envelope + empty state): a zero-hit search returns `count == 0` and
    `next[]` falls back to a search-refinement hint — never a `mail-get` built from a
    nonexistent row, and no crash.

No live mail/creds — everything below uses in-process fakes, matching the
CR-OA-020/CR-OA-026 §S1 fake-adapter pattern.
"""
import json
from argparse import Namespace

import pytest

import vidushi_oa._cli as cli
from vidushi_oa import toon as oa_toon
from vidushi_oa.mail.base import MailAdapter, Message
from vidushi_oa.mail.client import MailClient


def _msg(id_, account, source_tag, subject, sender, date, uid):
    return Message(
        id=id_, account=account, source_tag=source_tag, subject=subject,
        sender=sender, to="me@example.com", date=date, uid=uid, folder="INBOX",
    )


class FakeAdapter(MailAdapter):
    """No network — canned `Message`s, matching the CR-OA-020/026 fake-adapter
    pattern. `search` ignores the query text and returns whatever it was seeded
    with, so an empty seed list deterministically produces a zero-hit search."""

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


def _build_fake_client_with_hit():
    fastmail = FakeAdapter("fastmail", "[FM]", {"server_side_categories"}, messages=[ORDER_MSG])
    return MailClient({"fastmail": fastmail})


def _build_fake_client_with_no_hits():
    fastmail = FakeAdapter("fastmail", "[FM]", {"server_side_categories"}, messages=[])
    return MailClient({"fastmail": fastmail})


@pytest.fixture(autouse=True)
def restore_cli_fmt():
    original = getattr(cli, "_FMT", "toon")
    yield
    cli._FMT = original


def test_mail_search_default_projection_carries_uid_and_account_axi_guard(monkeypatch, capsys):
    """AXI #2 guard: uid/account are in the MINIMAL DEFAULT projection (no --full,
    no --fields) — not only under --full. This is expected to already pass off the
    §S1 GREEN; kept here as a conformance guard for §S2, not the primary RED."""
    client = _build_fake_client_with_hit()
    monkeypatch.setattr(cli, "build_client", lambda **kw: client)
    cli._FMT = "toon"

    cli.cmd_mail_search(Namespace(query="order", accounts=None, full=False))

    payload = oa_toon.from_toon(capsys.readouterr().out)
    assert payload["count"] == 1
    row = payload["results"][0]
    assert row["uid"] == "42", f"default TOON row must carry uid '42', got {row}"
    assert row["account"] == "fastmail", f"default TOON row must carry account 'fastmail', got {row}"


def test_mail_search_next_hint_is_a_runnable_mail_get_from_first_row(monkeypatch, capsys):
    """AXI #9: next[] must contain a RUNNABLE `mail-get --account <account> --uid
    <uid>` string built from the FIRST result row's actual account/uid values —
    assert the EXACT interpolated command. This FAILS today: current next[] is
    `["mail-search <query> --accounts <name>", "mail-accounts"]` with no mail-get
    hint whatsoever."""
    client = _build_fake_client_with_hit()
    monkeypatch.setattr(cli, "build_client", lambda **kw: client)
    cli._FMT = "toon"

    cli.cmd_mail_search(Namespace(query="order", accounts=None, full=False))

    payload = oa_toon.from_toon(capsys.readouterr().out)
    assert payload["count"] == 1
    first_row = payload["results"][0]
    expected_hint = f"mail-get --account {first_row['account']} --uid {first_row['uid']}"
    assert expected_hint == "mail-get --account fastmail --uid 42", (
        f"sanity check on fixture values failed, got {expected_hint!r}"
    )
    assert expected_hint in payload["next"], (
        f"next[] must contain the runnable hint {expected_hint!r} built from the "
        f"first row's real account/uid, got next={payload['next']!r}"
    )


def test_mail_search_next_hint_json_mode_bare_array_has_no_next(monkeypatch, capsys):
    """--json mode stays a bare array (no envelope, no next[]) — the runnable
    mail-get hint is a TOON-envelope concern only. Included so the §S2 fix can't be
    implemented by leaking `next` into --json."""
    client = _build_fake_client_with_hit()
    monkeypatch.setattr(cli, "build_client", lambda **kw: client)
    cli._FMT = "json"

    cli.cmd_mail_search(Namespace(query="order", accounts=None, full=False))

    raw = capsys.readouterr().out.strip()
    rows = json.loads(raw)
    assert isinstance(rows, list)
    assert "next" not in raw, f"--json must stay a bare array with no next[] leakage, got {raw!r}"


def test_mail_search_zero_hits_falls_back_to_refinement_hint_not_a_mail_get(monkeypatch, capsys):
    """AXI #1/#5: a zero-hit search returns the definitive empty state — `count ==
    0` — and `next[]` falls back to a search-refinement hint, NEVER a `mail-get`
    built from a nonexistent row (there is no first row to build it from), and no
    crash/traceback."""
    client = _build_fake_client_with_no_hits()
    monkeypatch.setattr(cli, "build_client", lambda **kw: client)
    cli._FMT = "toon"

    cli.cmd_mail_search(Namespace(query="no-such-order", accounts=None, full=False))

    payload = oa_toon.from_toon(capsys.readouterr().out)
    assert payload["count"] == 0, f"zero-hit search must report count 0, got {payload['count']}"
    assert payload["results"] == [], f"zero-hit search must report an empty results list, got {payload['results']}"
    assert isinstance(payload["next"], list) and len(payload["next"]) > 0, (
        f"zero-hit search must still offer a search-refinement next[] hint, got {payload.get('next')!r}"
    )
    for hint in payload["next"]:
        assert not hint.startswith("mail-get"), (
            f"zero-hit search must NOT build a mail-get hint from a nonexistent row, got {hint!r} in next={payload['next']!r}"
        )
