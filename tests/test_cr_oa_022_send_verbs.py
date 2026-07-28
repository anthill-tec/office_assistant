"""CR-OA-022 §S3 — draft-then-confirm verbs (`mail-draft`/`mail-send`/`mail-reply`) (RED).

Covers the three AXI-conformant `mail-*` verbs §S3 adds on top of the already-merged
§S1 (`send`-capability flag + `MailSender` adapter methods + `send_gate.ensure_send_capable`)
and §S2 (`vidushi_oa.mail.compose.compose`/`validate_from`):

  - `mail-draft`  composes (§S2) and saves a REAL draft via the account adapter's
                  `create_draft(raw_rfc822)`; emits a TOON status carrying the
                  `draft` id; performs **zero** network send (spec lines 40-42,
                  AC line 79).
  - `mail-send`   gates on `send_gate.ensure_send_capable(entry)`, then dispatches
                  **only that identified draft** via the adapter's `send_draft(draft_id)`;
                  emits the sent `message_id` (spec lines 43-44, AC line 80).
  - `mail-reply`  fetches the source message via the adapter's `fetch_message(uid)`,
                  composes a **threaded** reply (In-Reply-To/References from the
                  fetched `Message`), and saves it as a draft exactly like `mail-draft`
                  — zero send (spec lines 45-46).

None of `cli.cmd_mail_draft`, `cli.cmd_mail_send`, or `cli.cmd_mail_reply` exist yet,
so every behavioural test below fails with `AttributeError` at the `cli.cmd_mail_*`
call. The no-auto-send-invariant and caller-existence tests fail because the
functions/verbs are entirely absent from `vidushi_oa/_cli.py` and `--help`.

Design pinned here for GREEN (see also the final RED report):
  - Because ``from`` is a Python keyword, the `--from` flag's argparse `dest` is
    `from_addr` on every verb that takes it (`mail-draft`, `mail-reply`) — so a
    `Namespace` never needs an attribute literally named ``from``.
  - `cmd_mail_draft(a)` reads `a.account`/`a.from_addr`/`a.to`/`a.subject`/`a.body`
    (+ optional `a.cc`/`a.attach`/`a.case`), calls
    `compose(a.from_addr, a.to, a.subject, a.body, cc=a.cc)` (§S2), looks up the
    account's adapter via `client._adapters[a.account]` (the same seam
    `cmd_mail_get` uses), calls `adapter.create_draft(raw)`, and emits
    `{"status": "drafted", "draft": <draft_id>, "account": a.account}` through
    `out()` (TOON by default, matching `cmd_mail_auth`'s flat status-object
    convention — no `tally`/`next` envelope). It NEVER calls `send_draft`/
    `EmailSubmission/set`/`sendmail`.
  - `cmd_mail_send(a)` reads `a.account`/`a.draft`, resolves the account's REGISTRY
    entry via `vidushi_oa.mail.accounts.load_accounts()` (matched by `name`), calls
    `send_gate.ensure_send_capable(entry)` (raising `PermissionError` -> structured
    error + exit 1 for a non-send-capable account — exercised by a different cycle's
    §S1 tests, not here), then `adapter.send_draft(a.draft)`, and emits
    `{"status": "sent", "message_id": <message_id>, "draft": a.draft, "account": a.account}`.
    This is the ONLY function in `vidushi_oa/_cli.py` that may call a send-path token
    (`send_draft(`/`EmailSubmission/set`/`sendmail`).
  - `cmd_mail_reply(a)` reads `a.account`/`a.uid`/`a.from_addr`/`a.body` (+ optional
    `a.attach`/`a.case`), fetches the source via `adapter.fetch_message(a.uid)`,
    composes a threaded reply with `in_reply_to=source.id` (and `references=source.id`),
    then saves it as a draft exactly like `cmd_mail_draft` (same status shape) —
    zero send.

No live sending anywhere in this file — every adapter below is an in-process fake
(`RecordingAdapter`) whose `create_draft`/`send_draft` just count calls and record
what they received, per the CR's "tests run against fakes" acceptance-criteria note.
"""
import json
import os
import re
import subprocess
import sys
from argparse import Namespace

import pytest

import vidushi_oa._cli as cli
from vidushi_oa.mail.base import MailAdapter, Message
from vidushi_oa.mail.client import MailClient

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STORE = os.path.join(ROOT, "scripts", "store.py")
CLI_SRC = os.path.join(ROOT, "vidushi_oa", "_cli.py")

# The tokens that constitute an actual "send path" per AC §S3 line 81.
_SEND_TOKENS = ("send_draft(", "EmailSubmission/set", "sendmail")


def _msg(id_, account, source_tag, subject, sender, date, uid):
    return Message(
        id=id_, account=account, source_tag=source_tag, subject=subject,
        sender=sender, to="me@example.com", date=date, uid=uid, folder="INBOX",
    )


class RecordingAdapter(MailAdapter):
    """No network — a fake adapter that COUNTS `create_draft`/`send_draft` calls
    and records exactly what each received, so the draft-then-confirm invariant
    (exactly one draft-save on `mail-draft`/`mail-reply`, exactly one send on
    `mail-send`, and never the other way around) is mechanically checkable."""

    def __init__(self, account, source_tag, caps=None, messages=None):
        self.account = account
        self.source_tag = source_tag
        self._caps = set(caps or [])
        self._messages = messages if messages is not None else []
        self.draft_saves = 0
        self.sends = 0
        self.sent_draft_ids = []
        self._drafts = {}
        self._next_draft_id = 1

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

    def create_draft(self, raw_rfc822, folder="Drafts"):
        self.draft_saves += 1
        draft_id = f"draft-{self._next_draft_id}"
        self._next_draft_id += 1
        self._drafts[draft_id] = raw_rfc822
        return draft_id

    def send_draft(self, draft_id):
        self.sends += 1
        self.sent_draft_ids.append(draft_id)
        return f"sent-{draft_id}"


def _extract_func_body(src, name):
    """Return the body text of `def name(...):` up to (not including) the next
    top-level `def`, or `None` if `name` isn't defined at all in `src`."""
    pattern = re.compile(rf"\ndef {re.escape(name)}\(.*?\):\n(.*?)(?=\ndef |\Z)", re.DOTALL)
    match = pattern.search(src)
    return match.group(1) if match else None


@pytest.fixture(autouse=True)
def restore_cli_fmt():
    """`cmd_mail_*` reads the module-global `cli._FMT` — tests below mutate it
    directly, so restore it afterwards (matches CR-OA-020's mail-verb tests)."""
    original = getattr(cli, "_FMT", "toon")
    yield
    cli._FMT = original


def _isolate_backend(monkeypatch, tmp_path, name="oa"):
    """Point the sqlite backend + mail-accounts registry at throwaway tmp paths
    (mirrors `test_cr_oa_022_send_guards.py`) so the §S4 verified-recipient guard —
    which `cmd_mail_draft`/`cmd_mail_reply` now enforce — consults an isolated
    contacts store rather than the real one."""
    monkeypatch.setenv("VIDUSHI_BACKEND", "sqlite")
    monkeypatch.setenv("VIDUSHI_SQLITE_PATH", str(tmp_path / f"{name}.db"))
    monkeypatch.setenv("VIDUSHI_MAIL_CONFIG", str(tmp_path / "accounts.json"))


def _seed_contact(contact_id, support_email, vendor="Acme"):
    """Seed a verified `contact` whose `support_email` is the recipient under test,
    so the §S4 verified-recipient guard admits it — keeping these §S3 tests on the
    legitimately-verified path (never `--force`)."""
    from vidushi_oa.backends import get_backend
    store = get_backend().store("contacts")
    store.ensure_id_index()
    store.insert({"id": contact_id, "vendor": vendor, "support_email": support_email})


# --------------------------------------------------------------------------- #
# mail-draft — saves exactly one draft, zero sends
# --------------------------------------------------------------------------- #

def test_mail_draft_saves_exactly_one_draft_and_sends_zero(monkeypatch, capsys, tmp_path):
    _isolate_backend(monkeypatch, tmp_path)
    _seed_contact("ven_acme", "vendor@example.com")
    adapter = RecordingAdapter("gmail_main", "[GM]", caps={"send"})
    client = MailClient({"gmail_main": adapter})
    monkeypatch.setattr(cli, "build_client", lambda **kw: client)
    cli._FMT = "toon"

    cli.cmd_mail_draft(Namespace(
        account="gmail_main", from_addr="me@gmail.com", to="vendor@example.com",
        subject="Order query", body="Please help with my order.",
        cc=None, attach=None, case=None,
    ))

    assert adapter.draft_saves == 1, "mail-draft must save exactly one draft"
    assert adapter.sends == 0, "mail-draft must NEVER invoke a send path"

    from vidushi_oa import toon as oa_toon
    payload = oa_toon.from_toon(capsys.readouterr().out)
    assert payload["draft"] == "draft-1", f"TOON status must carry the draft id; got {payload!r}"
    assert payload["account"] == "gmail_main"

    raw = adapter._drafts["draft-1"]
    assert b"From: me@gmail.com" in raw
    assert b"To: vendor@example.com" in raw
    assert b"Subject: Order query" in raw


def test_mail_draft_json_mode_status_carries_the_draft_id_no_envelope(monkeypatch, capsys, tmp_path):
    _isolate_backend(monkeypatch, tmp_path)
    _seed_contact("ven_vendor", "support@vendor.com")
    adapter = RecordingAdapter("fastmail_main", "[FM]", caps={"send"})
    client = MailClient({"fastmail_main": adapter})
    monkeypatch.setattr(cli, "build_client", lambda **kw: client)
    cli._FMT = "json"

    cli.cmd_mail_draft(Namespace(
        account="fastmail_main", from_addr="me@fastmail.com", to="support@vendor.com",
        subject="RMA request", body="Need an RMA.", cc=None, attach=None, case=None,
    ))

    assert adapter.draft_saves == 1
    assert adapter.sends == 0

    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["draft"] == "draft-1"
    assert "tally" not in payload
    assert "next" not in payload


# --------------------------------------------------------------------------- #
# mail-send — sends exactly one identified draft
# --------------------------------------------------------------------------- #

def test_mail_send_triggers_exactly_one_send_draft_and_returns_message_id(monkeypatch, capsys, tmp_path):
    from vidushi_oa.mail import accounts

    config_path = tmp_path / "accounts.json"
    monkeypatch.setenv("VIDUSHI_MAIL_CONFIG", str(config_path))
    accounts.add_account("gmail_main", "gmail", "me@gmail.com", "keyring:gmail-main", send=True)

    adapter = RecordingAdapter("gmail_main", "[GM]", caps={"send"})
    client = MailClient({"gmail_main": adapter})
    monkeypatch.setattr(cli, "build_client", lambda **kw: client)
    cli._FMT = "toon"

    cli.cmd_mail_send(Namespace(account="gmail_main", draft="draft-42"))

    assert adapter.sends == 1, "mail-send must trigger exactly one send_draft call"
    assert adapter.sent_draft_ids == ["draft-42"], (
        f"mail-send must dispatch ONLY the identified draft; sent {adapter.sent_draft_ids!r}"
    )
    assert adapter.draft_saves == 0, "mail-send must never itself save a new draft"

    from vidushi_oa import toon as oa_toon
    payload = oa_toon.from_toon(capsys.readouterr().out)
    assert payload["message_id"] == "sent-draft-42", f"must return the sent message id; got {payload!r}"


def test_mail_send_json_mode_returns_the_exact_message_id(monkeypatch, capsys, tmp_path):
    from vidushi_oa.mail import accounts

    config_path = tmp_path / "accounts.json"
    monkeypatch.setenv("VIDUSHI_MAIL_CONFIG", str(config_path))
    accounts.add_account("fastmail_main", "fastmail", "me@fastmail.com", "keyring:fastmail-main", send=True)

    adapter = RecordingAdapter("fastmail_main", "[FM]", caps={"send"})
    client = MailClient({"fastmail_main": adapter})
    monkeypatch.setattr(cli, "build_client", lambda **kw: client)
    cli._FMT = "json"

    cli.cmd_mail_send(Namespace(account="fastmail_main", draft="draft-7"))

    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["message_id"] == "sent-draft-7"
    assert adapter.sent_draft_ids == ["draft-7"]
    assert adapter.sends == 1


# --------------------------------------------------------------------------- #
# mail-reply — builds a threaded draft, zero send
# --------------------------------------------------------------------------- #

def test_mail_reply_builds_a_threaded_draft_with_zero_sends(monkeypatch, capsys, tmp_path):
    _isolate_backend(monkeypatch, tmp_path)
    _seed_contact("ven_acme", "vendor@example.com")
    source = _msg("<m1@y>", "gmail_main", "[GM]", "Order Update",
                  "vendor@example.com", "2026-07-25T09:00:00Z", "src-1")
    adapter = RecordingAdapter("gmail_main", "[GM]", caps={"send"}, messages=[source])
    client = MailClient({"gmail_main": adapter})
    monkeypatch.setattr(cli, "build_client", lambda **kw: client)
    cli._FMT = "toon"

    cli.cmd_mail_reply(Namespace(
        account="gmail_main", uid="src-1", from_addr="me@gmail.com",
        body="Thanks, following up.", attach=None, case=None,
    ))

    assert adapter.draft_saves == 1, "mail-reply must save exactly one threaded draft"
    assert adapter.sends == 0, "mail-reply must NEVER invoke a send path"

    raw = next(iter(adapter._drafts.values()))
    assert b"In-Reply-To: <m1@y>" in raw, (
        f"the saved draft must be threaded to the fetched source's Message-ID; got {raw!r}"
    )

    from vidushi_oa import toon as oa_toon
    payload = oa_toon.from_toon(capsys.readouterr().out)
    assert payload["draft"] in adapter._drafts, f"TOON status must carry the draft id; got {payload!r}"


def test_mail_reply_unknown_source_uid_is_a_structured_error_not_a_traceback(monkeypatch, capsys):
    """`fetch_message` raising `KeyError` for an unknown uid (the same failure mode
    `mail-get` already handles) must surface as a structured error + exit 1, not a
    leaked traceback, and must never touch the send path."""
    adapter = RecordingAdapter("gmail_main", "[GM]", caps={"send"}, messages=[])
    client = MailClient({"gmail_main": adapter})
    monkeypatch.setattr(cli, "build_client", lambda **kw: client)
    cli._FMT = "json"

    with pytest.raises(SystemExit) as exc_info:
        cli.cmd_mail_reply(Namespace(
            account="gmail_main", uid="does-not-exist", from_addr="me@gmail.com",
            body="Following up.", attach=None, case=None,
        ))

    assert exc_info.value.code != 0
    captured = capsys.readouterr()
    assert "Traceback" not in captured.out and "Traceback" not in captured.err
    assert "error" in json.loads(captured.out.strip())
    assert adapter.draft_saves == 0
    assert adapter.sends == 0


# --------------------------------------------------------------------------- #
# No-auto-send invariant (mechanically auditable) — AC §S3 line 81
# --------------------------------------------------------------------------- #

def test_send_path_tokens_are_invoked_only_from_cmd_mail_send():
    with open(CLI_SRC, encoding="utf-8") as f:
        src = f.read()

    send_body = _extract_func_body(src, "cmd_mail_send")
    assert send_body is not None, "cmd_mail_send must be defined in vidushi_oa/_cli.py"
    assert any(tok in send_body for tok in _SEND_TOKENS), (
        f"cmd_mail_send must itself invoke a send-path token {list(_SEND_TOKENS)!r}"
    )

    for other_name in ("cmd_mail_draft", "cmd_mail_reply"):
        other_body = _extract_func_body(src, other_name)
        assert other_body is not None, f"{other_name} must be defined in vidushi_oa/_cli.py"
        for tok in _SEND_TOKENS:
            assert tok not in other_body, (
                f"{other_name} must NEVER invoke send-path token {tok!r} — draft-then-"
                f"confirm requires mail-send be the ONLY code path that can send"
            )


# --------------------------------------------------------------------------- #
# Caller-existence — AC §S3 line 82
# --------------------------------------------------------------------------- #

def test_help_lists_mail_draft_send_and_reply_verbs():
    result = subprocess.run([sys.executable, STORE, "--help"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    for verb in ("mail-draft", "mail-send", "mail-reply"):
        assert verb in result.stdout, f"--help must list {verb!r}; got:\n{result.stdout}"


def test_each_send_verb_is_wired_via_a_non_test_set_defaults_caller():
    with open(CLI_SRC, encoding="utf-8") as f:
        src = f.read()
    for func_name in ("cmd_mail_draft", "cmd_mail_send", "cmd_mail_reply"):
        assert src.count(func_name) >= 2, (
            f"{func_name} must be both defined and wired via a set_defaults caller "
            f"in vidushi_oa/_cli.py (found {src.count(func_name)} reference(s))"
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
