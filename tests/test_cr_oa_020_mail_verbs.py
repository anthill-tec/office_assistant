"""CR-OA-020 §S5 — `mail-*` verbs (AXI/TOON) (RED).

Covers the four AXI-conformant CLI verbs the mail subsystem exposes (spec lines 49-55,
AC lines 94-96):

  - `mail-auth`     register a credential *reference* (provider, address, secret-ref) —
                     NEVER the secret itself.
  - `mail-accounts` list configured accounts + their capabilities.
  - `mail-search`   server-side search across selected accounts -> merge + de-dup by
                     `Message-ID` + `[FM]`/`[GM]`/`[YH]` source-tag + field-project +
                     TOON envelope (`results`/`tally`/`next`); `--json` -> bare array,
                     no `tally`.
  - `mail-get`      one message by account+uid.

None of `vidushi_oa.mail.accounts`, `vidushi_oa.mail.factory`, or the four `cmd_mail_*`
CLI handlers (+ their `build_client` seam) exist yet, so every test below is RED:
`vidushi_oa.mail.accounts`/`factory` imports raise `ModuleNotFoundError`; `cli.cmd_mail_*`
/ `cli.build_client` attribute access raises `AttributeError`. Imports of those
not-yet-existing symbols happen INSIDE each test body (not at module level) so a
missing module/attribute fails only the test that needs it, not the whole file's
collection.

Design pinned here for GREEN (see also the final RED report):
  - `accounts.add_account(name, provider, address, secret_ref, path=None)` persists an
    entry with EXACTLY the keys `{name, provider, address, secret_ref}` — never a raw
    secret value. `accounts.load_accounts(path=None)` reads them back in append order.
    Path resolution: `VIDUSHI_MAIL_CONFIG` env var, else
    `$XDG_CONFIG_HOME/vidushi-oa/accounts.json`; the file is created mode `0600`.
  - `factory.build_client(config_path=None, resolver=None, adapter_factory=None)` reads
    the configured accounts, calls `adapter_factory(provider=, account=, address=,
    secret_ref=, resolver=)` per account (the test injection seam — the real
    implementation constructs `GmailImapAdapter`/`YahooImapAdapter`/`fastmail_adapter`),
    sets the returned adapter's `.source_tag` per provider (gmail->`[GM]`,
    yahoo->`[YH]`, fastmail->`[FM]`), and registers it on a `MailClient` under the
    account name. Secret resolution is LAZY: `build_client()` itself never calls
    `resolver.resolve(...)`.
  - `cli.build_client` is a module-level name in `vidushi_oa._cli` (importable/
    monkeypatchable) that `cmd_mail_search`/`cmd_mail_accounts`/`cmd_mail_get` call with
    no arguments to obtain a `MailClient`.
  - `cmd_mail_search(a)` reads `a.query` (+ optional `a.accounts`), calls
    `client.search(a.query, accounts=a.accounts)`, projects each row to EXACTLY
    `{id, source_tag, subject, sender, date}`, and (TOON) emits
    `{"count", "tally": {"source_tag": {tag: count, ...}}, "results": [...], "next": [...]}`
    — matching the existing `cmd_query` tally-nested-by-axis convention. `--json`
    (`cli._FMT == "json"`) emits a bare list of the same projected rows, no envelope.
  - `cmd_mail_accounts(a)` emits a TOON envelope with `results`: one row per registered
    account (`account` name + `capabilities` as a sorted list).
  - `cmd_mail_get(a)` reads `a.account`/`a.uid`, fetches via that account's adapter, and
    emits TOON `{"result": {...projected...}, "next": [...]}` / a bare object in JSON
    mode; an unknown account or uid is a structured `{"error": ...}` on stdout + exit 1
    (never a raw traceback), per AXI #6.

No live mail/creds — everything below uses in-process fakes.
"""
import json
import os
import stat
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


def _msg(id_, account, source_tag, subject, sender, date, uid):
    return Message(
        id=id_, account=account, source_tag=source_tag, subject=subject,
        sender=sender, to="me@example.com", date=date, uid=uid, folder="INBOX",
    )


class FakeAdapter(MailAdapter):
    """No network — canned `Message`s + canned capabilities, plus a `fetch_message`
    that raises `KeyError` for an unknown uid (mirrors a real adapter's failure mode
    so `cmd_mail_get`'s error path can be exercised)."""

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


SHARED_ID = "<shared-abc@example.com>"

GM_ONE = _msg("<gm-1@gmail.com>", "gmail_main", "[GM]", "GM One", "a@gm.com", "2026-07-20T10:00:00Z", "gm-1")
GM_SHARED = _msg(SHARED_ID, "gmail_main", "[GM]", "Shared via GM", "a@gm.com", "2026-07-21T09:00:00Z", "gm-2")
FM_ONE = _msg("<fm-1@fastmail.com>", "fastmail_main", "[FM]", "FM One", "b@fastmail.com", "2026-07-19T08:00:00Z", "fm-1")
FM_SHARED = _msg(SHARED_ID, "fastmail_main", "[FM]", "Shared via FM", "b@fastmail.com", "2026-07-21T09:00:00Z", "fm-2")
YH_ONE = _msg("<yh-1@yahoo.com>", "yahoo_main", "[YH]", "YH One", "c@yahoo.com", "2026-07-18T07:00:00Z", "yh-1")


def _build_fake_client():
    """3 fake accounts ([GM]/[FM]/[YH]); GM and FM each also carry a message sharing
    `SHARED_ID` so the client-level dedup collapses them to one row (GM's, since
    gmail_main is registered — and therefore dispatched — first)."""
    gmail = FakeAdapter("gmail_main", "[GM]", {"raw_query", "server_threads"}, messages=[GM_ONE, GM_SHARED])
    fastmail = FakeAdapter("fastmail_main", "[FM]", {"server_side_categories"}, messages=[FM_ONE, FM_SHARED])
    yahoo = FakeAdapter("yahoo_main", "[YH]", {"legacy_only"}, messages=[YH_ONE])
    return MailClient({"gmail_main": gmail, "fastmail_main": fastmail, "yahoo_main": yahoo})


@pytest.fixture(autouse=True)
def restore_cli_fmt():
    """`cmd_mail_*` reads the module-global `cli._FMT` (matching every other `cmd_*`
    in `_cli.py`) — tests below mutate it directly, so restore it afterwards."""
    original = getattr(cli, "_FMT", "toon")
    yield
    cli._FMT = original


# ─────────────────────────── mail-search (direct-call, fakes) ───────────────────────────

def test_mail_search_toon_envelope_has_results_tally_and_next(monkeypatch, capsys):
    client = _build_fake_client()
    monkeypatch.setattr(cli, "build_client", lambda **kw: client)
    cli._FMT = "toon"

    cli.cmd_mail_search(Namespace(query="invoice", accounts=None))

    out = capsys.readouterr().out
    assert "results[" in out, f"TOON envelope must carry results[]; got {out!r}"
    assert "tally" in out, f"TOON envelope must carry a tally; got {out!r}"
    assert "next" in out, f"TOON envelope must carry next[]; got {out!r}"


def test_mail_search_every_row_is_source_tagged_and_shared_id_deduped(monkeypatch, capsys):
    client = _build_fake_client()
    monkeypatch.setattr(cli, "build_client", lambda **kw: client)
    cli._FMT = "json"  # bare array is the easiest shape to introspect precisely

    cli.cmd_mail_search(Namespace(query="invoice", accounts=None))

    rows = json.loads(capsys.readouterr().out.strip())
    assert isinstance(rows, list)
    # 5 raw messages (2 GM + 2 FM + 1 YH) minus 1 collapsed duplicate = 4 rows.
    assert len(rows) == 4, f"expected the shared Message-ID to collapse to one row, got {rows}"

    for row in rows:
        assert row["source_tag"] in ("[FM]", "[GM]", "[YH]"), f"unexpected source_tag: {row}"

    shared_rows = [r for r in rows if r["id"] == SHARED_ID]
    assert len(shared_rows) == 1, f"two accounts returning the same Message-ID must de-dup to ONE row, got {shared_rows}"


def test_mail_search_row_projection_is_exactly_id_tag_subject_sender_date(monkeypatch, capsys):
    client = _build_fake_client()
    monkeypatch.setattr(cli, "build_client", lambda **kw: client)
    cli._FMT = "json"

    cli.cmd_mail_search(Namespace(query="invoice", accounts=None))

    rows = json.loads(capsys.readouterr().out.strip())
    gm_one_row = next(r for r in rows if r["id"] == GM_ONE.id)
    assert set(gm_one_row.keys()) == {"id", "source_tag", "subject", "sender", "date"}
    assert gm_one_row["subject"] == "GM One"
    assert gm_one_row["sender"] == "a@gm.com"
    assert gm_one_row["source_tag"] == "[GM]"


def test_mail_search_toon_tally_counts_rows_by_source_tag_after_dedup(monkeypatch, capsys):
    client = _build_fake_client()
    monkeypatch.setattr(cli, "build_client", lambda **kw: client)
    cli._FMT = "toon"

    cli.cmd_mail_search(Namespace(query="invoice", accounts=None))

    # Round-trip the TOON output back through the project's own decoder so the
    # assertion checks real values, not brittle substring matching.
    from vidushi_oa import toon as oa_toon
    out = capsys.readouterr().out
    payload = oa_toon.from_toon(out)

    assert payload["count"] == 4
    # Post-dedup: gm-one + shared(GM) + fm-one + yh-one. Tally keys are the
    # bracket-free provider tags ("GM"/"FM"/"YH") even though each ROW's own
    # `source_tag` keeps its bracketed form (e.g. "[GM]") — see the row-level
    # assertions above/elsewhere in this file, which are unchanged.
    assert payload["tally"] == {"source_tag": {"GM": 2, "FM": 1, "YH": 1}}
    assert isinstance(payload["next"], list)


def test_mail_search_json_mode_yields_bare_array_with_no_tally_or_next(monkeypatch, capsys):
    client = _build_fake_client()
    monkeypatch.setattr(cli, "build_client", lambda **kw: client)
    cli._FMT = "json"

    cli.cmd_mail_search(Namespace(query="invoice", accounts=None))

    raw = capsys.readouterr().out.strip()
    payload = json.loads(raw)
    assert isinstance(payload, list), f"--json must yield a bare array, got {type(payload)}: {raw!r}"
    assert len(payload) == 4
    assert "tally" not in raw
    assert "next" not in raw


# ─────────────────────────── mail-accounts (direct-call, fakes) ───────────────────────────

def test_mail_accounts_lists_every_registered_account_with_its_capabilities(monkeypatch, capsys):
    client = _build_fake_client()
    monkeypatch.setattr(cli, "build_client", lambda **kw: client)
    cli._FMT = "json"

    cli.cmd_mail_accounts(Namespace())

    payload = json.loads(capsys.readouterr().out.strip())
    results = payload["results"] if isinstance(payload, dict) else payload
    by_account = {row["account"]: row for row in results}
    assert set(by_account.keys()) == {"gmail_main", "fastmail_main", "yahoo_main"}
    assert sorted(by_account["gmail_main"]["capabilities"]) == sorted(["raw_query", "server_threads"])
    assert sorted(by_account["fastmail_main"]["capabilities"]) == ["server_side_categories"]


# ─────────────────────────── mail-get (direct-call, fakes) ───────────────────────────

def test_mail_get_happy_path_fetches_the_exact_message(monkeypatch, capsys):
    client = _build_fake_client()
    monkeypatch.setattr(cli, "build_client", lambda **kw: client)
    cli._FMT = "json"

    cli.cmd_mail_get(Namespace(account="gmail_main", uid="gm-1"))

    payload = json.loads(capsys.readouterr().out.strip())
    result = payload["result"] if "result" in payload else payload
    assert result["id"] == GM_ONE.id
    assert result["subject"] == "GM One"
    assert result["source_tag"] == "[GM]"


def test_mail_get_toon_mode_wraps_result_with_a_next_block(monkeypatch, capsys):
    client = _build_fake_client()
    monkeypatch.setattr(cli, "build_client", lambda **kw: client)
    cli._FMT = "toon"

    cli.cmd_mail_get(Namespace(account="yahoo_main", uid="yh-1"))

    from vidushi_oa import toon as oa_toon
    payload = oa_toon.from_toon(capsys.readouterr().out)
    assert payload["result"]["id"] == YH_ONE.id
    assert isinstance(payload["next"], list)


def test_mail_get_unknown_uid_is_a_structured_error_not_a_traceback(monkeypatch, capsys):
    client = _build_fake_client()
    monkeypatch.setattr(cli, "build_client", lambda **kw: client)
    cli._FMT = "json"

    with pytest.raises(SystemExit) as exc_info:
        cli.cmd_mail_get(Namespace(account="gmail_main", uid="does-not-exist"))

    assert exc_info.value.code != 0
    captured = capsys.readouterr()
    assert "Traceback" not in captured.out and "Traceback" not in captured.err
    payload = json.loads(captured.out.strip())
    assert "error" in payload


def test_mail_get_imap_none_return_for_unknown_uid_is_a_structured_error(monkeypatch, capsys):
    """The real `ImapAdapter.fetch_message` returns None (not KeyError) for an unknown
    uid — `cmd_mail_get` must still emit a structured error + exit 1, no traceback."""
    from vidushi_oa.mail.imap import ImapAdapter

    class _EmptyIMAP:
        def login(self, user, password):
            return ("OK", [b"Logged in"])

        def select(self, mailbox="INBOX", readonly=False):
            return ("OK", [b"1"])

        def uid(self, command, *args):
            return ("OK", [])  # FETCH of an unknown uid yields no message -> None

    adapter = ImapAdapter("gmail_main", "[GM]", host="imap.example.com",
                          user="me", password="pw",
                          conn_factory=lambda host, port: _EmptyIMAP())
    monkeypatch.setattr(cli, "build_client", lambda **kw: MailClient({"gmail_main": adapter}))
    cli._FMT = "json"

    with pytest.raises(SystemExit) as exc_info:
        cli.cmd_mail_get(Namespace(account="gmail_main", uid="404"))

    assert exc_info.value.code != 0
    captured = capsys.readouterr()
    assert "Traceback" not in captured.out and "Traceback" not in captured.err
    assert json.loads(captured.out.strip())["error"] == "message not found"


def test_mail_get_jmap_not_implemented_is_a_structured_error(monkeypatch, capsys):
    """`JmapAdapter.fetch_message` raises `NotImplementedError` — `cmd_mail_get` must
    render that as a structured error + exit 1, not a leaked traceback."""
    from vidushi_oa.mail.jmap import JmapAdapter

    adapter = JmapAdapter("fastmail_main", "[FM]", token="tok", transport=lambda *a, **k: (200, {}))
    monkeypatch.setattr(cli, "build_client", lambda **kw: MailClient({"fastmail_main": adapter}))
    cli._FMT = "json"

    with pytest.raises(SystemExit) as exc_info:
        cli.cmd_mail_get(Namespace(account="fastmail_main", uid="1"))

    assert exc_info.value.code != 0
    captured = capsys.readouterr()
    assert "Traceback" not in captured.out and "Traceback" not in captured.err
    assert "error" in json.loads(captured.out.strip())


def test_mail_get_unknown_account_is_a_structured_error(monkeypatch, capsys):
    client = _build_fake_client()
    monkeypatch.setattr(cli, "build_client", lambda **kw: client)
    cli._FMT = "json"

    with pytest.raises(SystemExit) as exc_info:
        cli.cmd_mail_get(Namespace(account="no_such_account", uid="1"))

    assert exc_info.value.code != 0
    captured = capsys.readouterr()
    assert "Traceback" not in captured.out and "Traceback" not in captured.err
    assert "error" in json.loads(captured.out.strip())


# ─────────────────────────── vidushi_oa.mail.accounts (reference-only registry) ───────────

def test_add_account_then_load_accounts_round_trips_a_reference_only_entry(tmp_path, monkeypatch):
    from vidushi_oa.mail import accounts

    config_path = tmp_path / "accounts.json"
    monkeypatch.setenv("VIDUSHI_MAIL_CONFIG", str(config_path))

    accounts.add_account("fastmail_main", "fastmail", "user@fastmail.com", "keyring:fastmail-main")
    loaded = accounts.load_accounts()

    assert loaded == [{
        "name": "fastmail_main",
        "provider": "fastmail",
        "address": "user@fastmail.com",
        "secret_ref": "keyring:fastmail-main",
        "auth_mode": "password",
    }]


def test_add_account_appends_without_clobbering_earlier_entries(tmp_path, monkeypatch):
    from vidushi_oa.mail import accounts

    config_path = tmp_path / "accounts.json"
    monkeypatch.setenv("VIDUSHI_MAIL_CONFIG", str(config_path))

    accounts.add_account("gmail_main", "gmail", "user@gmail.com", "keyring:gmail-main")
    accounts.add_account("yahoo_main", "yahoo", "user@yahoo.com", "keyring:yahoo-main")

    loaded = accounts.load_accounts()
    assert [e["name"] for e in loaded] == ["gmail_main", "yahoo_main"]


def test_load_accounts_returns_empty_list_when_config_file_is_absent(tmp_path, monkeypatch):
    from vidushi_oa.mail import accounts

    monkeypatch.setenv("VIDUSHI_MAIL_CONFIG", str(tmp_path / "does-not-exist.json"))

    assert accounts.load_accounts() == []


def test_accounts_file_is_created_with_mode_0600(tmp_path, monkeypatch):
    from vidushi_oa.mail import accounts

    config_path = tmp_path / "accounts.json"
    monkeypatch.setenv("VIDUSHI_MAIL_CONFIG", str(config_path))

    accounts.add_account("gmail_main", "gmail", "user@gmail.com", "keyring:gmail-main")

    assert config_path.exists()
    mode = stat.S_IMODE(os.stat(config_path).st_mode)
    assert mode == 0o600, f"accounts.json must be created 0600, got {oct(mode)}"


def test_accounts_file_contains_exactly_the_reference_only_schema_no_secret_material(tmp_path, monkeypatch):
    from vidushi_oa.mail import accounts

    sentinel = "SENTINEL-b3f14c7d-must-never-be-persisted"
    config_path = tmp_path / "accounts.json"
    monkeypatch.setenv("VIDUSHI_MAIL_CONFIG", str(config_path))

    accounts.add_account("gmail_main", "gmail", "user@gmail.com", "op://vault/item/gmail")

    raw = config_path.read_text(encoding="utf-8")
    assert sentinel not in raw

    loaded = accounts.load_accounts()
    assert len(loaded) == 1
    assert set(loaded[0].keys()) == {"name", "provider", "address", "secret_ref", "auth_mode"}


# ─────────────────────────── cmd_mail_auth (direct-call) ───────────────────────────

def test_cmd_mail_auth_persists_only_a_reference_never_a_secret(tmp_path, monkeypatch, capsys):
    from vidushi_oa.mail import accounts

    config_path = tmp_path / "accounts.json"
    monkeypatch.setenv("VIDUSHI_MAIL_CONFIG", str(config_path))
    cli._FMT = "toon"

    cli.cmd_mail_auth(Namespace(provider="fastmail", address="user@fastmail.com",
                                 secret_ref="keyring:fastmail-main"))

    loaded = accounts.load_accounts()
    assert len(loaded) == 1
    entry = loaded[0]
    assert entry["provider"] == "fastmail"
    assert entry["address"] == "user@fastmail.com"
    assert entry["secret_ref"] == "keyring:fastmail-main"
    assert entry["auth_mode"] == "password"
    assert set(entry.keys()) == {"name", "provider", "address", "secret_ref", "auth_mode"}

    captured = capsys.readouterr().out
    assert "fastmail" in captured
    assert "user@fastmail.com" in captured


def test_cmd_mail_auth_json_mode_emits_a_bare_status_object(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "accounts.json"
    monkeypatch.setenv("VIDUSHI_MAIL_CONFIG", str(config_path))
    cli._FMT = "json"

    cli.cmd_mail_auth(Namespace(provider="yahoo", address="user@yahoo.com",
                                 secret_ref="keyring:yahoo-main"))

    payload = json.loads(capsys.readouterr().out.strip())
    assert isinstance(payload, dict)
    assert "next" not in payload
    assert "tally" not in payload


def test_cmd_mail_auth_rejects_an_unsupported_provider(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "accounts.json"
    monkeypatch.setenv("VIDUSHI_MAIL_CONFIG", str(config_path))
    cli._FMT = "json"

    with pytest.raises(SystemExit) as exc_info:
        cli.cmd_mail_auth(Namespace(provider="aol", address="user@aol.com",
                                     secret_ref="keyring:aol-main"))

    assert exc_info.value.code != 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert "error" in payload


# ─────────────────────────── vidushi_oa.mail.factory (build_client) ───────────────────────

def test_build_client_wires_adapters_by_provider_and_stamps_the_right_source_tag(tmp_path, monkeypatch):
    from vidushi_oa.mail import accounts, factory

    config_path = tmp_path / "accounts.json"
    monkeypatch.setenv("VIDUSHI_MAIL_CONFIG", str(config_path))
    accounts.add_account("gmail_main", "gmail", "user@gmail.com", "keyring:gmail-main")
    accounts.add_account("yahoo_main", "yahoo", "user@yahoo.com", "keyring:yahoo-main")
    accounts.add_account("fastmail_main", "fastmail", "user@fastmail.com", "keyring:fastmail-main")

    seen_providers = {}

    def fake_adapter_factory(provider, account, address, secret_ref, resolver,
                             auth_mode="password"):
        seen_providers[account] = provider
        return FakeAdapter(account, "", set())

    resolver = object()
    client = factory.build_client(resolver=resolver, adapter_factory=fake_adapter_factory)

    assert seen_providers == {
        "gmail_main": "gmail", "yahoo_main": "yahoo", "fastmail_main": "fastmail",
    }
    registered = dict(client.accounts())
    assert set(registered.keys()) == {"gmail_main", "yahoo_main", "fastmail_main"}
    assert client._adapters["gmail_main"].source_tag == "[GM]"
    assert client._adapters["yahoo_main"].source_tag == "[YH]"
    assert client._adapters["fastmail_main"].source_tag == "[FM]"


def test_build_client_never_eagerly_resolves_the_secret(tmp_path, monkeypatch):
    from unittest import mock

    from vidushi_oa.mail import accounts, factory

    config_path = tmp_path / "accounts.json"
    monkeypatch.setenv("VIDUSHI_MAIL_CONFIG", str(config_path))
    accounts.add_account("gmail_main", "gmail", "user@gmail.com", "keyring:gmail-main")

    def fake_adapter_factory(provider, account, address, secret_ref, resolver,
                             auth_mode="password"):
        return FakeAdapter(account, "", set())

    resolver = mock.Mock()
    factory.build_client(resolver=resolver, adapter_factory=fake_adapter_factory)

    resolver.resolve.assert_not_called()


# ─────────────────────────── caller-existence (subprocess + source grep) ───────────────

def test_help_lists_all_four_mail_verbs():
    result = subprocess.run([sys.executable, STORE, "--help"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    for verb in ("mail-search", "mail-auth", "mail-accounts", "mail-get"):
        assert verb in result.stdout, f"--help must list {verb!r}; got:\n{result.stdout}"


def test_each_mail_verb_is_wired_via_a_non_test_set_defaults_caller():
    with open(CLI_SRC, encoding="utf-8") as f:
        src = f.read()
    for func_name in ("cmd_mail_auth", "cmd_mail_accounts", "cmd_mail_search", "cmd_mail_get"):
        assert src.count(func_name) >= 2, (
            f"{func_name} must be both defined and wired via a set_defaults caller "
            f"in vidushi_oa/_cli.py (found {src.count(func_name)} reference(s))"
        )


if __name__ == "__main__":
    import pytest as _pytest
    raise SystemExit(_pytest.main([__file__]))
