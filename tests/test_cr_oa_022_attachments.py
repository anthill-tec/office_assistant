"""CR-OA-022 §S6 — attachments (RED).

`mail-draft --attach <path>` is meant to read the file at `<path>` and include it as
an attachment: the composed draft becomes a `multipart` message whose attachment
part carries the file's filename and its bytes (AC §S6 line 91).

Today `cmd_mail_draft` in `vidushi_oa/_cli.py` only ever calls
``compose(a.from_addr, a.to, a.subject, a.body, cc=a.cc)`` — `a.attach` is parsed by
argparse (`mdr.add_argument("--attach", default=None)`) but NEVER read: it is not
passed through to `compose(..., attachments=...)`, so no file is ever opened and no
attachment part is ever added, no matter what `--attach` is given. `compose()` itself
(§S2, already merged) already supports an `attachments` kwarg shaped as a list of
``(filename, bytes)`` pairs and adds each as an ``application/octet-stream`` part via
`EmailMessage.add_attachment` — so GREEN's job is purely to wire
`cmd_mail_draft`/`cmd_mail_reply` to read `a.attach` and pass
``attachments=[(basename, file_bytes)]`` through to `compose(...)`.

Test 1 below (`test_mail_draft_attach_yields_multipart_draft_with_filename_and_bytes`)
FAILS today: the captured raw draft is a single-part text message, never multipart,
so `msg.is_multipart()` is False and no attachment part exists.

Test 2 is a guard pinning the OTHER half of the AC: omitting `--attach` must never
force a multipart draft — `--attach` is what introduces the attachment, not some
GREEN side effect that always wraps the body in a multipart envelope. This already
holds true against today's code (which does nothing with `--attach` either way), so
it does not itself flip red->green; it exists so a GREEN that carelessly always
multiparts the message (e.g. unconditionally wrapping the body) is caught as a
regression the moment §S6 lands.

FAKES ONLY — a `RecordingAdapter` (mirroring `tests/test_cr_oa_022_send_verbs.py`)
stands in for the provider adapter; its `create_draft` just records the raw RFC 5322
bytes it received, which is parsed back with `email.message_from_bytes` so these
tests assert against the REAL wire format rather than any internal shape.
"""
import email
import json
import os

import pytest
from argparse import Namespace

import vidushi_oa._cli as cli
from vidushi_oa.mail.base import MailAdapter
from vidushi_oa.mail.client import MailClient

ATTACHMENT_BYTES = b"%PDF-1.4-FAKE\n" + (b"CR-OA-022-S6-ATTACHMENT-BYTES-" * 20)


class RecordingAdapter(MailAdapter):
    """No network — a fake adapter that records exactly the raw bytes each
    `create_draft` call received (mirrors `tests/test_cr_oa_022_send_verbs.py`)."""

    def __init__(self, account, source_tag, caps=None, messages=None):
        self.account = account
        self.source_tag = source_tag
        self._caps = set(caps or [])
        self._messages = messages if messages is not None else []
        self.draft_saves = 0
        self.sends = 0
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
        return f"sent-{draft_id}"


@pytest.fixture(autouse=True)
def restore_cli_fmt():
    """`cmd_mail_draft` reads the module-global `cli._FMT` — restore it afterwards
    (matches `tests/test_cr_oa_022_send_verbs.py`)."""
    original = getattr(cli, "_FMT", "toon")
    yield
    cli._FMT = original


def _isolate_backend(monkeypatch, tmp_path, name="oa"):
    """Point the sqlite backend + mail-accounts registry at throwaway tmp paths
    (mirrors `tests/test_cr_oa_022_send_verbs.py`) so the §S4 verified-recipient
    guard consults an isolated contacts store rather than the real one."""
    monkeypatch.setenv("VIDUSHI_BACKEND", "sqlite")
    monkeypatch.setenv("VIDUSHI_SQLITE_PATH", str(tmp_path / f"{name}.db"))
    monkeypatch.setenv("VIDUSHI_MAIL_CONFIG", str(tmp_path / "accounts.json"))


def _seed_contact(contact_id, support_email, vendor="Acme"):
    """Seed a verified `contact` whose `support_email` is the recipient under test,
    so the §S4 verified-recipient guard admits it (never `--force`)."""
    from vidushi_oa.backends import get_backend
    store = get_backend().store("contacts")
    store.ensure_id_index()
    store.insert({"id": contact_id, "vendor": vendor, "support_email": support_email})


def _find_attachment_part(msg, filename):
    for part in msg.walk():
        if part.get_content_disposition() == "attachment" and part.get_filename() == filename:
            return part
    return None


def test_mail_draft_attach_yields_multipart_draft_with_filename_and_bytes(
    monkeypatch, tmp_path
):
    """`mail-draft --attach <path>` must read the file at <path> and attach it: the
    captured raw draft must be multipart, and among its parts an attachment part
    must carry the file's basename as filename and its exact bytes as payload."""
    _isolate_backend(monkeypatch, tmp_path)
    _seed_contact("ven_acme", "vendor@example.com")

    doc_path = tmp_path / "2026-01-01_acme_invoice_123.pdf"
    doc_path.write_bytes(ATTACHMENT_BYTES)

    adapter = RecordingAdapter("gmail_main", "[GM]", caps={"send"})
    client = MailClient({"gmail_main": adapter})
    monkeypatch.setattr(cli, "build_client", lambda **kw: client)
    cli._FMT = "toon"

    cli.cmd_mail_draft(Namespace(
        account="gmail_main", from_addr="me@gmail.com", to="vendor@example.com",
        subject="Invoice attached", body="Please find the invoice attached.",
        cc=None, attach=str(doc_path), case=None,
    ))

    assert adapter.draft_saves == 1, "mail-draft must save exactly one draft"
    assert adapter.sends == 0, "mail-draft must NEVER invoke a send path"

    raw = adapter._drafts["draft-1"]
    parsed = email.message_from_bytes(raw)

    assert parsed.is_multipart(), (
        "a draft composed with --attach must be a multipart message; got a "
        f"single-part message with Content-Type {parsed.get_content_type()!r}"
    )

    expected_filename = os.path.basename(str(doc_path))
    part = _find_attachment_part(parsed, expected_filename)
    assert part is not None, (
        f"no attachment part named {expected_filename!r} found among parts: "
        f"{[p.get_filename() for p in parsed.walk()]!r}"
    )

    payload = part.get_payload(decode=True)
    assert len(payload) == len(ATTACHMENT_BYTES), (
        f"attachment payload length must match the file's byte length "
        f"({len(ATTACHMENT_BYTES)}); got {len(payload)}"
    )
    assert payload == ATTACHMENT_BYTES, (
        "attachment payload bytes must match the written file's bytes exactly"
    )


def test_mail_draft_without_attach_is_not_multipart(monkeypatch, tmp_path):
    """Omitting --attach must never force a multipart envelope — --attach is what
    introduces the attachment, not a side effect GREEN applies unconditionally. This
    pins the OTHER half of the AC as a regression guard alongside the test above."""
    _isolate_backend(monkeypatch, tmp_path)
    _seed_contact("ven_acme", "vendor@example.com")

    adapter = RecordingAdapter("gmail_main", "[GM]", caps={"send"})
    client = MailClient({"gmail_main": adapter})
    monkeypatch.setattr(cli, "build_client", lambda **kw: client)
    cli._FMT = "toon"

    cli.cmd_mail_draft(Namespace(
        account="gmail_main", from_addr="me@gmail.com", to="vendor@example.com",
        subject="No attachment here", body="Just checking in.",
        cc=None, attach=None, case=None,
    ))

    assert adapter.draft_saves == 1
    assert adapter.sends == 0

    raw = adapter._drafts["draft-1"]
    parsed = email.message_from_bytes(raw)

    assert not parsed.is_multipart(), (
        "a draft composed WITHOUT --attach must not be multipart; got Content-Type "
        f"{parsed.get_content_type()!r} with parts {[p.get_filename() for p in parsed.walk()]!r}"
    )
    assert _find_attachment_part(parsed, None) is None


def test_mail_draft_attach_unreadable_file_exits_1_with_structured_error(
    monkeypatch, tmp_path, capsys
):
    """`mail-draft --attach <nonexistent/unreadable path>` must fail with a
    structured `{"error":"attachment not readable", ...}` and exit 1 — never a raw
    traceback (covers the `_attachments_or_exit` OSError branch)."""
    _isolate_backend(monkeypatch, tmp_path)
    _seed_contact("ven_acme", "vendor@example.com")

    missing_path = tmp_path / "does_not_exist" / "ghost.pdf"

    adapter = RecordingAdapter("gmail_main", "[GM]", caps={"send"})
    client = MailClient({"gmail_main": adapter})
    monkeypatch.setattr(cli, "build_client", lambda **kw: client)
    cli._FMT = "json"

    with pytest.raises(SystemExit) as exc_info:
        cli.cmd_mail_draft(Namespace(
            account="gmail_main", from_addr="me@gmail.com", to="vendor@example.com",
            subject="Invoice attached", body="Please find the invoice attached.",
            cc=None, attach=str(missing_path), case=None,
        ))

    assert exc_info.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "attachment not readable"
    assert payload["path"] == str(missing_path)
    assert "reason" in payload
    # No draft was ever saved and no send path was reached on the error branch.
    assert adapter.draft_saves == 0
    assert adapter.sends == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
