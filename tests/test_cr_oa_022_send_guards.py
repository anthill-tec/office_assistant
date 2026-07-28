"""CR-OA-022 §S4 — verified-recipient + From-identity guards on `mail-draft`/`mail-reply` (RED).

Per CR-OA-022 §S4 and DN-mail-access.md §Decision 7 ("Guards (mirror the skill). Draft/send
only to a verified `contact` (else a structured error unless explicitly overridden)... The
From is a chosen identity — the account address or a configured Fastmail masked alias —
validated against the account's identities; an unknown From is refused."):

  - **Verified-recipient guard.** The `contacts` store (`vidushi_oa/schema/contacts.schema.json`,
    field `support_email` — see `data/schema.md`: "verified address only; null = TBD") is the
    recipient allow-list. `mail-draft --to <not-any-contact's-support_email>` must exit 1 with
    a structured error naming the unverified recipient; `--force` must let it through.
  - **From-identity guard.** The account's identities are its registered `address` plus a
    configured `aliases` list (persisted on the account entry, set via a repeatable
    `mail-auth --alias` flag). `--from` outside that set must exit 1 with a structured error;
    the account address and any configured alias must always be accepted.
  - `mail-reply` must honor the verified-recipient guard on the reply target too.

None of this exists today:
  - `cmd_mail_draft`/`cmd_mail_reply` in `vidushi_oa/_cli.py` never consult the `contacts`
    store and never call `vidushi_oa.mail.compose.validate_from` — every test below that
    expects a `SystemExit` on a bad recipient/From currently proceeds to save a draft and
    exit 0 instead, so those assertions fail today.
  - `vidushi_oa.mail.accounts.add_account` has no `aliases` parameter (CR-OA-020's fixed
    signature plus CR-OA-022 §S1's `send` flag only) — passing `aliases=[...]` raises
    `TypeError` today.
  - `voa mail-draft`/`voa mail-reply` have no `--force` flag and `voa mail-auth` has no
    `--alias` flag — argparse rejects both today (non-zero exit), asserted directly for the
    mail-auth --alias case (subprocess) and exercised indirectly for --force by passing it as
    a plain `Namespace` attribute to the `cmd_mail_draft`/`cmd_mail_reply` functions directly
    (bypassing argparse) so the *guard logic* itself is what's under test.
  - Two tests use a lightweight "spy" wrapper around the real `sqlite` backend / the real
    `compose.validate_from` to prove the guard actually CONSULTS the contacts allow-list /
    the identity validator (not just "happens not to crash") — a no-op stub that merely lets
    everything through would still fail these, since the spy would record zero calls.

FAKES ONLY — a `RecordingAdapter` (mirroring `tests/test_cr_oa_022_send_verbs.py`) stands in
for every provider adapter; the `contacts`/`accounts` stores are the real embedded backends
pointed at an isolated tmp sqlite file / tmp accounts.json per test (never the real store).
"""
import json
import os
import subprocess
import sys
from argparse import Namespace

import pytest

import vidushi_oa._cli as cli
from vidushi_oa.backends import get_backend as _real_get_backend
from vidushi_oa.mail import accounts
from vidushi_oa.mail.base import MailAdapter, Message
from vidushi_oa.mail.client import MailClient

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STORE = os.path.join(ROOT, "scripts", "store.py")


def _msg(id_, account, source_tag, subject, sender, date, uid):
    return Message(
        id=id_, account=account, source_tag=source_tag, subject=subject,
        sender=sender, to="me@example.com", date=date, uid=uid, folder="INBOX",
    )


class RecordingAdapter(MailAdapter):
    """No network — a fake adapter that COUNTS `create_draft`/`send_draft` calls and
    records exactly what each received (mirrors `test_cr_oa_022_send_verbs.py`)."""

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


class _SpyContactsStore:
    """Wraps a real `Store`, recording every `find`/`find_one` call it receives (into a
    SHARED sink list, since a fresh spy is minted per `get_backend().store(...)` call)
    while still delegating to the real backend — proves the guard genuinely CONSULTED
    the allow-list rather than merely letting everything through."""

    def __init__(self, real_store, sink):
        self._real = real_store
        self._sink = sink

    def find(self, query, fields=None, extra=None):
        self._sink.append(query)
        return self._real.find(query, fields=fields, extra=extra)

    def find_one(self, query, fields=None):
        self._sink.append(query)
        return self._real.find_one(query, fields=fields)

    def __getattr__(self, name):
        return getattr(self._real, name)


class _SpyBackend:
    """Wraps the real active backend, returning a `_SpyContactsStore` (sharing one
    `sink` list across every mint) only for the `contacts` type; every other store
    passes straight through untouched."""

    def __init__(self, real_backend, sink):
        self._real = real_backend
        self._sink = sink

    def store(self, type_):
        real_store = self._real.store(type_)
        return _SpyContactsStore(real_store, self._sink) if type_ == "contacts" else real_store

    def check(self):
        return self._real.check()


def _isolate_backend(monkeypatch, tmp_path, name="oa"):
    """Point the sqlite backend + mail-accounts registry at throwaway tmp paths."""
    monkeypatch.setenv("VIDUSHI_BACKEND", "sqlite")
    monkeypatch.setenv("VIDUSHI_SQLITE_PATH", str(tmp_path / f"{name}.db"))
    monkeypatch.setenv("VIDUSHI_MAIL_CONFIG", str(tmp_path / "accounts.json"))


def _seed_contact(contact_id, support_email, vendor="Acme"):
    store = _real_get_backend().store("contacts")
    store.ensure_id_index()
    store.insert({"id": contact_id, "vendor": vendor, "support_email": support_email})


@pytest.fixture(autouse=True)
def restore_cli_fmt():
    """`cmd_mail_*` reads the module-global `cli._FMT` — tests below mutate it directly,
    so restore it afterwards (matches CR-OA-020/022's mail-verb tests)."""
    original = getattr(cli, "_FMT", "toon")
    yield
    cli._FMT = original


# 1. Verified-recipient guard: blocks an unverified `--to`, `--force` overrides it.

def test_mail_draft_to_unverified_recipient_blocks_unless_forced(monkeypatch, capsys, tmp_path):
    _isolate_backend(monkeypatch, tmp_path)
    # A real contact exists, but its support_email is a DIFFERENT address than the one
    # we draft to below — proves the guard checks equality, not merely "any contact exists".
    _seed_contact("ven_acme", "other@acme.com")

    adapter = RecordingAdapter("gmail_main", "[GM]", caps={"send"})
    client = MailClient({"gmail_main": adapter})
    monkeypatch.setattr(cli, "build_client", lambda **kw: client)
    cli._FMT = "json"

    draft_kwargs = dict(
        account="gmail_main", from_addr="me@gmail.com", to="unverified@evil.com",
        subject="Order query", body="Please help.", cc=None, attach=None, case=None,
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.cmd_mail_draft(Namespace(force=False, **draft_kwargs))

    assert exc_info.value.code != 0, "an unverified recipient must exit non-zero"
    assert adapter.draft_saves == 0, "an unverified recipient must not be drafted without --force"
    payload = json.loads(capsys.readouterr().out.strip())
    assert "error" in payload, f"must be a structured error payload; got {payload!r}"
    assert "unverified@evil.com" in payload["error"], (
        f"structured error must name the unverified recipient; got {payload!r}"
    )
    err_lower = payload["error"].lower()
    assert "verified" in err_lower or "contact" in err_lower, (
        f"structured error must reference verification/contacts; got {payload!r}"
    )

    # --force lets the SAME unverified recipient through.
    cli.cmd_mail_draft(Namespace(force=True, **draft_kwargs))
    assert adapter.draft_saves == 1, "--force must let the unverified recipient's draft through"
    assert adapter.sends == 0, "mail-draft must still never send"
    forced_payload = json.loads(capsys.readouterr().out.strip())
    assert forced_payload.get("draft") == "draft-1"


# 2. Verified recipient is allowed through without needing --force, AND the guard really
#    consulted the contacts allow-list (spy) rather than being a no-op that lets everything
#    through regardless.

def test_mail_draft_to_a_verified_contact_saves_without_needing_force(monkeypatch, capsys, tmp_path):
    _isolate_backend(monkeypatch, tmp_path)
    _seed_contact("ven_acme", "verified@acme.com")

    contacts_queries = []
    monkeypatch.setattr(
        "vidushi_oa.backends.get_backend",
        lambda name=None: _SpyBackend(_real_get_backend(name), contacts_queries),
    )

    adapter = RecordingAdapter("gmail_main", "[GM]", caps={"send"})
    client = MailClient({"gmail_main": adapter})
    monkeypatch.setattr(cli, "build_client", lambda **kw: client)
    cli._FMT = "json"

    cli.cmd_mail_draft(Namespace(
        account="gmail_main", from_addr="me@gmail.com", to="verified@acme.com",
        subject="Order query", body="Please help.", cc=None, attach=None, case=None,
        force=False,
    ))

    assert adapter.draft_saves == 1, "a verified recipient must save the draft without --force"
    assert adapter.sends == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert "error" not in payload, f"a verified recipient must not raise a guard error; got {payload!r}"
    assert payload.get("draft") == "draft-1"
    assert contacts_queries, (
        "mail-draft must consult the contacts store's support_email allow-list before "
        "accepting a recipient — the contacts store was never queried, so this guard "
        "isn't wired (a no-op stub that lets everything through would also fail here)"
    )


# 3. From-identity guard: blocks a `--from` that is neither the account address nor a
#    configured alias.

def test_mail_draft_from_a_non_identity_address_blocks_with_structured_error(monkeypatch, capsys, tmp_path):
    _isolate_backend(monkeypatch, tmp_path)
    _seed_contact("ven_acme", "verified@acme.com")
    accounts.add_account("gmail_main", "gmail", "me@gmail.com", "keyring:gmail-main", send=True)

    adapter = RecordingAdapter("gmail_main", "[GM]", caps={"send"})
    client = MailClient({"gmail_main": adapter})
    monkeypatch.setattr(cli, "build_client", lambda **kw: client)
    cli._FMT = "json"

    with pytest.raises(SystemExit) as exc_info:
        cli.cmd_mail_draft(Namespace(
            account="gmail_main", from_addr="stranger@evil.com", to="verified@acme.com",
            subject="Order query", body="Please help.", cc=None, attach=None, case=None,
            force=False,
        ))

    assert exc_info.value.code != 0, "an unknown From identity must exit non-zero"
    assert adapter.draft_saves == 0, "an invalid From must never save a draft"
    payload = json.loads(capsys.readouterr().out.strip())
    assert "error" in payload, f"must be a structured error payload; got {payload!r}"
    assert "stranger@evil.com" in payload["error"], (
        f"structured error must name the invalid From; got {payload!r}"
    )


# 4. From-identity guard: the account's own address is ALWAYS accepted — and the guard
#    really consults `compose.validate_from` (spy) to enforce it, rather than being absent.

def test_mail_draft_from_the_registered_account_address_is_always_accepted(monkeypatch, capsys, tmp_path):
    _isolate_backend(monkeypatch, tmp_path)
    _seed_contact("ven_acme", "verified@acme.com")
    accounts.add_account("gmail_main", "gmail", "me@gmail.com", "keyring:gmail-main", send=True)

    from vidushi_oa.mail import compose as compose_module
    real_validate_from = compose_module.validate_from
    calls = []

    def _spy_validate_from(from_addr, identities):
        calls.append((from_addr, set(identities)))
        return real_validate_from(from_addr, identities)

    monkeypatch.setattr(compose_module, "validate_from", _spy_validate_from)

    adapter = RecordingAdapter("gmail_main", "[GM]", caps={"send"})
    client = MailClient({"gmail_main": adapter})
    monkeypatch.setattr(cli, "build_client", lambda **kw: client)
    cli._FMT = "json"

    cli.cmd_mail_draft(Namespace(
        account="gmail_main", from_addr="me@gmail.com", to="verified@acme.com",
        subject="Order query", body="Please help.", cc=None, attach=None, case=None,
        force=False,
    ))

    assert adapter.draft_saves == 1, "the account's own address must always be accepted as From"
    assert calls, (
        "cmd_mail_draft must call vidushi_oa.mail.compose.validate_from(from_addr, identities) "
        "to enforce the From-identity guard; it was never invoked"
    )
    from_addr_seen, identities_seen = calls[0]
    assert from_addr_seen == "me@gmail.com"
    assert "me@gmail.com" in identities_seen, (
        f"the account's own address must be a member of its identities set; got {identities_seen!r}"
    )


# 4b. From-identity guard: a configured alias is accepted too (draft saved).

def test_mail_draft_from_a_configured_alias_is_accepted(monkeypatch, capsys, tmp_path):
    _isolate_backend(monkeypatch, tmp_path)
    _seed_contact("ven_acme", "verified@acme.com")
    accounts.add_account("gmail_main", "gmail", "me@gmail.com", "keyring:gmail-main",
                         send=True, aliases=["alias@gmail.com"])

    adapter = RecordingAdapter("gmail_main", "[GM]", caps={"send"})
    client = MailClient({"gmail_main": adapter})
    monkeypatch.setattr(cli, "build_client", lambda **kw: client)
    cli._FMT = "json"

    cli.cmd_mail_draft(Namespace(
        account="gmail_main", from_addr="alias@gmail.com", to="verified@acme.com",
        subject="Order query", body="Please help.", cc=None, attach=None, case=None,
        force=False,
    ))

    assert adapter.draft_saves == 1, "a configured alias must be accepted as From"
    payload = json.loads(capsys.readouterr().out.strip())
    assert "error" not in payload, f"a configured alias must not raise a guard error; got {payload!r}"
    assert payload.get("draft") == "draft-1"


# 5. `mail-auth --alias` persists a repeatable alias list on the account entry.

def test_add_account_persists_aliases_list_when_given(tmp_path):
    """Direct `accounts.add_account` behaviour — the `aliases` parameter groundwork."""
    path = tmp_path / "accounts.json"
    entry = accounts.add_account(
        "gmail:alias@x.com", "gmail", "alias@x.com", "ref1",
        aliases=["a@x.com", "b@x.com"], path=str(path),
    )

    assert entry.get("aliases") == ["a@x.com", "b@x.com"]
    stored = accounts.load_accounts(str(path))
    assert stored[0].get("aliases") == ["a@x.com", "b@x.com"]


def test_add_account_defaults_aliases_to_empty_list_when_not_specified(tmp_path):
    path = tmp_path / "accounts.json"
    entry = accounts.add_account(
        "gmail:noalias@x.com", "gmail", "noalias@x.com", "ref2", path=str(path),
    )

    assert entry.get("aliases") == [], (
        "an entry with no explicit aliases must default to an empty list, not omit "
        "the field or leave it unset"
    )


def test_mail_auth_with_repeated_alias_flags_persists_an_ordered_alias_list(tmp_path):
    accounts_path = tmp_path / "accounts.json"
    env = dict(os.environ)
    env["VIDUSHI_MAIL_CONFIG"] = str(accounts_path)
    env["VIDUSHI_FORMAT"] = "json"

    result = subprocess.run(
        [sys.executable, STORE, "mail-auth", "--provider", "gmail",
         "--address", "s@x.com", "--secret-ref", "vidushi-oa/gmail:s@x.com",
         "--send", "--alias", "a@x.com", "--alias", "b@x.com"],
        capture_output=True, text=True, env=env,
    )

    assert result.returncode == 0, (
        f"mail-auth --alias must be a recognised repeatable flag; stderr={result.stderr!r}"
    )
    with open(accounts_path, encoding="utf-8") as f:
        entries = json.load(f)
    assert len(entries) == 1
    assert entries[0].get("aliases") == ["a@x.com", "b@x.com"], (
        f"repeated --alias flags must persist as an ordered list; got {entries[0]!r}"
    )


def test_mail_auth_without_alias_flag_persists_an_empty_alias_list(tmp_path):
    accounts_path = tmp_path / "accounts.json"
    env = dict(os.environ)
    env["VIDUSHI_MAIL_CONFIG"] = str(accounts_path)
    env["VIDUSHI_FORMAT"] = "json"

    result = subprocess.run(
        [sys.executable, STORE, "mail-auth", "--provider", "gmail",
         "--address", "r@x.com", "--secret-ref", "vidushi-oa/gmail:r@x.com"],
        capture_output=True, text=True, env=env,
    )

    assert result.returncode == 0, f"stderr={result.stderr!r}"
    with open(accounts_path, encoding="utf-8") as f:
        entries = json.load(f)
    assert entries[0].get("aliases") == [], (
        "mail-auth without --alias must persist an empty alias list, not omit the field"
    )


# 6. `mail-reply` honors the verified-recipient guard on the reply target too.

def test_mail_reply_to_an_unverified_source_sender_blocks_unless_forced(monkeypatch, capsys, tmp_path):
    _isolate_backend(monkeypatch, tmp_path)
    _seed_contact("ven_acme", "verified@acme.com")

    source = _msg("<m1@y>", "gmail_main", "[GM]", "Order Update",
                  "unverified-sender@evil.com", "2026-07-25T09:00:00Z", "src-1")
    adapter = RecordingAdapter("gmail_main", "[GM]", caps={"send"}, messages=[source])
    client = MailClient({"gmail_main": adapter})
    monkeypatch.setattr(cli, "build_client", lambda **kw: client)
    cli._FMT = "json"

    reply_kwargs = dict(
        account="gmail_main", uid="src-1", from_addr="me@gmail.com",
        body="Following up.", attach=None, case=None,
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.cmd_mail_reply(Namespace(force=False, **reply_kwargs))

    assert exc_info.value.code != 0, "a reply to an unverified source sender must exit non-zero"
    assert adapter.draft_saves == 0, "an unverified reply target must not be drafted without --force"
    assert adapter.sends == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert "error" in payload, f"must be a structured error payload; got {payload!r}"
    assert "unverified-sender@evil.com" in payload["error"], (
        f"structured error must name the unverified sender; got {payload!r}"
    )

    # --force lets the reply through to the SAME unverified sender.
    cli.cmd_mail_reply(Namespace(force=True, **reply_kwargs))
    assert adapter.draft_saves == 1, "--force must let the reply to an unverified sender through"
    assert adapter.sends == 0, "mail-reply must still never send"
    forced_payload = json.loads(capsys.readouterr().out.strip())
    assert forced_payload.get("draft") in adapter._drafts


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
