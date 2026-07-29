"""CR-OA-022 §S5 — store linkage: the tracked correspondence trail (RED).

Per CR-OA-022 §S5 and DN-mail-access.md §Decision 7 ("Guards (mirror the skill) ...
a message can be linked to a store row (`--case`/`--invoice`/…) and, on send, recorded
as a `document` + the relevant action resolved on that row — the tracked correspondence
trail."):

  - `mail-draft`/`mail-reply` already PARSE `--case` (see `_cli.py` argparse setup) but
    today it is a dead argument — `cmd_mail_draft`/`cmd_mail_reply` never persist it
    anywhere, so nothing links a draft id back to the case it was drafted for.
  - `mail-send` of a linked draft must, after `adapter.send_draft(draft_id)` returns a
    message id: (a) record a **document** on the linked row referencing that message id,
    and (b) resolve the mapped **correspondence action** on that row (for `cases`, the
    `raise-ticket` action per `ACTION_SETS["cases"]` in `_cli.py`) — then clear the link.
  - An unlinked draft (`--case` never given) must leave every store row untouched on send.

None of this exists today:
  - `vidushi_oa.mail.draft_links` (the small link-persistence module `save_link(draft_id,
    fk_field, fk_id)` / `pop_link(draft_id) -> {fk_field, fk_id} | None`) does not exist —
    importing it raises `ModuleNotFoundError`.
  - `cmd_mail_draft` never calls `save_link`, so no link is ever persisted for a
    `--case`-drafted message.
  - `cmd_mail_send` never looks up a link, never pushes a `document` onto the linked
    row, and never resolves the mapped action — a case's `documents`/`actions` are
    untouched by any `mail-send` call today.

Design pinned here for GREEN:
  - Link persistence: `vidushi_oa.mail.draft_links.save_link(draft_id, fk_field, fk_id)`
    persists `fk_field` (the STORE TYPE the FK targets, e.g. `"cases"`) and `fk_id` (the
    row id, e.g. `"case_x"`) keyed by `draft_id`; `pop_link(draft_id)` returns
    `{"fk_field": ..., "fk_id": ...}` and removes the entry, or `None` if no link exists
    for that draft id (e.g. an unlinked draft, or a link already consumed by a prior
    `mail-send`).
  - `cmd_mail_draft`/`cmd_mail_reply`, when given `--case <id>`, call
    `save_link(draft_id, "cases", <id>)` after `adapter.create_draft(raw)` returns the
    draft id.
  - `cmd_mail_send`, after `adapter.send_draft(a.draft)` returns `message_id`, calls
    `pop_link(a.draft)`; if a link exists, it pushes
    `{"type": "correspondence", "message_id": <message_id>}` onto the linked row's
    `documents` array (the same `push` mechanics `cmd_doc_add` uses) and resolves the
    mapped correspondence action for that store type (`"cases" -> "raise-ticket"`, i.e.
    the same `resolve=("actions", ...)` mechanics `cmd_action_resolve` uses) — flipping
    a seeded OPEN `raise-ticket` action to `RESOLVED` with a `resolved` date. An unlinked
    draft (`pop_link` returns `None`) leaves every store row untouched.

FAKES ONLY — a `RecordingAdapter` (mirroring `test_cr_oa_022_send_verbs.py` /
`test_cr_oa_022_send_guards.py`) stands in for the provider adapter; `contacts`/`cases`
are the real embedded sqlite backend pointed at an isolated tmp sqlite file per test, and
`accounts.json` is pointed at an isolated tmp path — never the real store.
"""
import json
from argparse import Namespace

import pytest

import vidushi_oa._cli as cli
from vidushi_oa.backends import get_backend as _real_get_backend
from vidushi_oa.backends import query as Q
from vidushi_oa.mail import accounts
from vidushi_oa.mail.base import MailAdapter
from vidushi_oa.mail.client import MailClient


class RecordingAdapter(MailAdapter):
    """No network — a fake adapter that counts `create_draft`/`send_draft` calls and
    records exactly what each received (mirrors the §S3/§S4 RED test adapters)."""

    def __init__(self, account, source_tag, caps=None):
        self.account = account
        self.source_tag = source_tag
        self._caps = set(caps or [])
        self.draft_saves = 0
        self.sends = 0
        self.sent_draft_ids = []
        self._drafts = {}
        self._next_draft_id = 1

    def capabilities(self):
        return set(self._caps)

    def search(self, query, folder=None, limit=None):
        return []

    def fetch_message(self, uid, folder=None):
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


def _isolate_backend(monkeypatch, tmp_path, name="oa"):
    """Point the sqlite backend + mail-accounts registry at throwaway tmp paths
    (mirrors `test_cr_oa_022_send_guards.py`)."""
    monkeypatch.setenv("VIDUSHI_BACKEND", "sqlite")
    monkeypatch.setenv("VIDUSHI_SQLITE_PATH", str(tmp_path / f"{name}.db"))
    monkeypatch.setenv("VIDUSHI_MAIL_CONFIG", str(tmp_path / "accounts.json"))


def _seed_contact(contact_id, support_email, vendor="Acme"):
    store = _real_get_backend().store("contacts")
    store.ensure_id_index()
    store.insert({"id": contact_id, "vendor": vendor, "support_email": support_email})


def _seed_case(case_id, actions=None, documents=None, vendor="Acme"):
    store = _real_get_backend().store("cases")
    store.ensure_id_index()
    store.insert({
        "id": case_id,
        "vendor": vendor,
        "status": "IN_PROGRESS",
        "actions": actions if actions is not None else [],
        "documents": documents if documents is not None else [],
    })


def _get_case(case_id):
    return _real_get_backend().store("cases").find_one(Q.cond("id", "eq", case_id))


def _draft_kwargs(case=None, to="verified@acme.com"):
    return dict(
        account="gmail_main", from_addr="me@gmail.com", to=to,
        subject="Order query", body="Please help.", cc=None, attach=None,
        case=case, force=False,
    )


@pytest.fixture(autouse=True)
def restore_cli_fmt():
    """`cmd_mail_*` reads the module-global `cli._FMT`; restore it after each test
    (matches the §S3/§S4 RED test fixtures)."""
    original = getattr(cli, "_FMT", "toon")
    yield
    cli._FMT = original


def _make_client(monkeypatch, account="gmail_main", caps=("send",)):
    adapter = RecordingAdapter(account, "[GM]", caps=set(caps))
    client = MailClient({account: adapter})
    monkeypatch.setattr(cli, "build_client", lambda **kw: client)
    return adapter


# 1. Link persisted on draft: `mail-draft --case case_x` persists a draft_id -> case_x
#    link the tests can retrieve via `draft_links.pop_link`.

def test_mail_draft_with_case_fk_persists_a_draft_link(monkeypatch, capsys, tmp_path):
    from vidushi_oa.mail import draft_links  # does not exist yet -> ModuleNotFoundError (RED)

    _isolate_backend(monkeypatch, tmp_path)
    _seed_contact("ven_acme", "verified@acme.com")
    _seed_case("case_x")
    _make_client(monkeypatch)
    cli._FMT = "json"

    cli.cmd_mail_draft(Namespace(**_draft_kwargs(case="case_x")))

    payload = json.loads(capsys.readouterr().out.strip())
    draft_id = payload["draft"]
    assert draft_id == "draft-1"

    link = draft_links.pop_link(draft_id)
    assert link is not None, (
        "mail-draft --case case_x must persist a draft_id -> case_x link via "
        "draft_links.save_link; pop_link found nothing for this draft id"
    )
    assert link["fk_field"] == "cases", f"expected fk_field 'cases'; got {link!r}"
    assert link["fk_id"] == "case_x", f"expected fk_id 'case_x'; got {link!r}"


# 2. Document recorded on send: `mail-send` of a linked draft pushes a document onto
#    the linked case referencing the sent message id.

def test_mail_send_of_linked_draft_records_a_document_with_the_message_id(monkeypatch, capsys, tmp_path):
    _isolate_backend(monkeypatch, tmp_path)
    _seed_contact("ven_acme", "verified@acme.com")
    _seed_case("case_x", actions=[{"action": "raise-ticket", "status": "OPEN", "opened": "2026-07-20"}])
    accounts.add_account("gmail_main", "gmail", "me@gmail.com", "keyring:gmail-main", send=True)
    _make_client(monkeypatch)
    cli._FMT = "json"

    cli.cmd_mail_draft(Namespace(**_draft_kwargs(case="case_x")))
    draft_payload = json.loads(capsys.readouterr().out.strip())
    draft_id = draft_payload["draft"]

    cli.cmd_mail_send(Namespace(account="gmail_main", draft=draft_id))
    send_payload = json.loads(capsys.readouterr().out.strip())
    message_id = send_payload["message_id"]
    assert message_id == f"sent-{draft_id}"

    case_row = _get_case("case_x")
    docs = case_row.get("documents") or []
    matches = [d for d in docs if d.get("message_id") == message_id]
    assert len(matches) == 1, (
        f"mail-send of a draft linked via --case case_x must push exactly one document "
        f"referencing the sent message id {message_id!r} onto case_x's documents; "
        f"got documents={docs!r}"
    )
    assert matches[0].get("type") == "correspondence", (
        f"the pushed document must be typed 'correspondence'; got {matches[0]!r}"
    )


# 3. Correspondence action resolved: the same send flips the seeded OPEN `raise-ticket`
#    action on case_x to RESOLVED (mirrors `cmd_action_resolve`'s mechanics).

def test_mail_send_of_linked_draft_resolves_the_open_raise_ticket_action(monkeypatch, capsys, tmp_path):
    _isolate_backend(monkeypatch, tmp_path)
    _seed_contact("ven_acme", "verified@acme.com")
    _seed_case("case_x", actions=[{"action": "raise-ticket", "status": "OPEN", "opened": "2026-07-20"}])
    accounts.add_account("gmail_main", "gmail", "me@gmail.com", "keyring:gmail-main", send=True)
    _make_client(monkeypatch)
    cli._FMT = "json"

    cli.cmd_mail_draft(Namespace(**_draft_kwargs(case="case_x")))
    draft_id = json.loads(capsys.readouterr().out.strip())["draft"]

    cli.cmd_mail_send(Namespace(account="gmail_main", draft=draft_id))
    capsys.readouterr()  # drain send's stdout, not under test here

    case_row = _get_case("case_x")
    raise_ticket_actions = [a for a in (case_row.get("actions") or []) if a.get("action") == "raise-ticket"]
    assert len(raise_ticket_actions) == 1, (
        f"expected exactly one raise-ticket action on case_x; got {case_row.get('actions')!r}"
    )
    resolved = raise_ticket_actions[0]
    assert resolved.get("status") == "RESOLVED", (
        f"mail-send of a --case-linked draft must resolve case_x's OPEN raise-ticket "
        f"action; got {resolved!r}"
    )
    assert "resolved" in resolved, (
        f"a resolved action must carry a 'resolved' date, matching cmd_action_resolve's "
        f"own mechanics; got {resolved!r}"
    )
    assert resolved.get("opened") == "2026-07-20", (
        "resolving must not clobber the action's original 'opened' date"
    )


# 3b. AXI #9 chain across the linked flow: the draft's TOON `next[]` is the runnable
#     confirm-and-send step (what SKILL.md tells the agent to copy verbatim), and the
#     linked send's `next[]` hands the agent back to the row the correspondence landed on.

def test_linked_draft_then_send_toon_next_chain_is_runnable(monkeypatch, capsys, tmp_path):
    from vidushi_oa import toon as oa_toon

    _isolate_backend(monkeypatch, tmp_path)
    _seed_contact("ven_acme", "verified@acme.com")
    _seed_case("case_x", actions=[{"action": "raise-ticket", "status": "OPEN", "opened": "2026-07-20"}])
    accounts.add_account("gmail_main", "gmail", "me@gmail.com", "keyring:gmail-main", send=True)
    _make_client(monkeypatch)
    cli._FMT = "toon"

    cli.cmd_mail_draft(Namespace(**_draft_kwargs(case="case_x")))
    draft_payload = oa_toon.from_toon(capsys.readouterr().out)
    draft_id = draft_payload["draft"]
    assert draft_payload["next"] == [f"mail-send --account gmail_main --draft {draft_id}"], (
        f"the draft's next[] must be the runnable confirm-and-send step; got {draft_payload!r}"
    )

    cli.cmd_mail_send(Namespace(account="gmail_main", draft=draft_id))
    send_payload = oa_toon.from_toon(capsys.readouterr().out)
    assert send_payload["next"] == ["get cases case_x"], (
        "a FK-linked send must point the agent at the row the correspondence was "
        f"recorded on; got {send_payload!r}"
    )


# 4. Unlinked send is inert: after a LINKED send has already recorded its document on
#    case_x (proving the linkage mechanism is actually wired — this is what makes the
#    test genuinely fail today, not vacuously pass), a SUBSEQUENT `mail-send` of a
#    draft created WITHOUT any FK must not add anything further to case_x (guards
#    against over-eager linkage / a stale link leaking across drafts / a naive
#    "always touch the last-used case" implementation).

def test_mail_send_of_unlinked_draft_after_a_linked_send_does_not_touch_case_row_again(monkeypatch, capsys, tmp_path):
    _isolate_backend(monkeypatch, tmp_path)
    _seed_contact("ven_acme", "verified@acme.com")
    _seed_case("case_x", actions=[{"action": "raise-ticket", "status": "OPEN", "opened": "2026-07-20"}])
    accounts.add_account("gmail_main", "gmail", "me@gmail.com", "keyring:gmail-main", send=True)
    _make_client(monkeypatch)
    cli._FMT = "json"

    # First: a LINKED draft+send actually links to case_x and records its document.
    cli.cmd_mail_draft(Namespace(**_draft_kwargs(case="case_x")))
    draft_1 = json.loads(capsys.readouterr().out.strip())["draft"]
    cli.cmd_mail_send(Namespace(account="gmail_main", draft=draft_1))
    capsys.readouterr()

    case_after_first = _get_case("case_x")
    docs_after_first = case_after_first.get("documents") or []
    assert len(docs_after_first) == 1, (
        f"sanity check that linkage actually happened first: expected exactly one "
        f"document on case_x after the linked send; got {docs_after_first!r}"
    )

    # Second: an UNLINKED draft+send must add nothing further to case_x.
    cli.cmd_mail_draft(Namespace(**_draft_kwargs(case=None)))
    draft_2 = json.loads(capsys.readouterr().out.strip())["draft"]
    assert draft_2 != draft_1
    cli.cmd_mail_send(Namespace(account="gmail_main", draft=draft_2))
    send_payload = json.loads(capsys.readouterr().out.strip())
    assert send_payload["message_id"] == f"sent-{draft_2}"

    case_after_second = _get_case("case_x")
    docs_after_second = case_after_second.get("documents") or []
    assert docs_after_second == docs_after_first, (
        f"an unlinked mail-send must never add another document to a previously-linked "
        f"case row; before={docs_after_first!r} after={docs_after_second!r}"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
