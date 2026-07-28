"""CR-OA-028 §S4/§S5 — `voa mail-extract` verb (AXI) (RED).

Covers the new `voa mail-extract --account <name> --uid <uid>` verb (spec lines
"§S4 `voa mail-extract` verb" / "§S5 AXI conformance", AC "§S4/§S5"):

  - fetches the message body via the account's adapter (`adapter.fetch_html_body`,
    §S1 — already merged), extracts schema.org entities (`extract_schema_org`, §S2 —
    already merged), maps them to store candidates (`to_store_candidates`, §S3 —
    already merged), and returns them as an AXI TOON envelope
    `{count, results, next}`.
  - Read-only: NO autonomous store write. `next[]` only *suggests* the exact
    `voa add <type> --json '<candidate>'` for the agent to run.
  - No markup -> the definitive empty state (`count: 0`, `results: []`, exit 0,
    NOT an error) so the skill falls back to heuristic extraction.
  - The raw HTML body is never surfaced in the envelope/output.
  - An unknown account is a structured error + exit 1 (no traceback), matching
    the `cmd_mail_get`/`_mail_adapter_or_exit` contract.

`cmd_mail_extract` does not exist yet in `vidushi_oa._cli`, so every test that calls
it fails today with `AttributeError: module 'vidushi_oa._cli' has no attribute
'cmd_mail_extract'`. Imports/attribute access happen inside each test body (not at
module level) so a missing symbol fails only the test that needs it.

Design pinned here for GREEN (see also the final RED report):
  - `cmd_mail_extract(a)` reads `a.account`/`a.uid`, resolves the adapter via
    `client._adapters.get(a.account)` (same seam as `cmd_mail_get`); an unknown
    account renders `{"error": ..., "account": a.account, "uid": a.uid}` + exit 1,
    no traceback.
  - `html = adapter.fetch_html_body(a.uid)`; `entities =
    extract_schema_org(html or "")`; `candidates = to_store_candidates(entities)`.
  - TOON envelope: `{"count": len(candidates), "results": candidates, "next": [...]}`.
    `--json` emits the bare `candidates` list (no envelope), matching the
    `mail-search`/`mail-accounts` `--json` convention.
  - Non-empty `candidates`: `next[0]` is exactly
    `f"voa add {candidates[0]['type']} --json '{json.dumps(candidates[0]['candidate'])}'"`.
  - Empty `candidates` (no markup): `next` is a fallback hint list whose first
    entry mentions "heuristic" (case-insensitive) — NOT an error, exit 0.
  - No store write of any kind: `cmd_mail_extract` never touches
    `vidushi_oa.backends.get_backend`.

No live mail/creds — everything below uses in-process fakes; no real order/personal
data (fictitious merchant/order numbers only).
"""
import json
import os
import subprocess
import sys
from argparse import Namespace
from unittest import mock

import pytest

import vidushi_oa._cli as cli
from vidushi_oa.mail.base import MailAdapter
from vidushi_oa.mail.client import MailClient

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STORE = os.path.join(ROOT, "scripts", "store.py")
CLI_SRC = os.path.join(ROOT, "vidushi_oa", "_cli.py")

RAW_BODY_SENTINEL = "RAW-BODY-SENTINEL-b17c4f9a-must-never-leave-the-engine"

# An artificial (no-personal-data) order-confirmation HTML embedding a schema.org
# `Order` JSON-LD block, plus a sentinel prose string that must never surface in
# `mail-extract` output (only the structured candidate fields may leave the engine).
ORDER_JSONLD_HTML = f"""<html><body>
<p>{RAW_BODY_SENTINEL} — thanks for your order, human-readable text nobody parses.</p>
<script type="application/ld+json">
{{"@context": "https://schema.org", "@type": "Order",
 "orderNumber": "ORD-2026-0042",
 "seller": {{"@type": "Organization", "name": "Acme Corp"}},
 "orderedItem": [{{"@type": "OrderItem", "orderedItem": {{"@type": "Product", "name": "Widget Deluxe"}}}}],
 "orderDate": "2026-07-20",
 "orderStatus": "https://schema.org/OrderShipped"}}
</script>
</body></html>"""

# No schema.org markup at all -> the definitive empty state.
PLAIN_HTML = "<html><body><p>Thanks for shopping with us! No tracking info yet.</p></body></html>"

EXPECTED_ORDER_CANDIDATE = {
    "number": "ORD-2026-0042",
    "merchant": "Acme Corp",
    "items": ["Widget Deluxe"],
    "order_date": "2026-07-20",
    "status": "IN_PROGRESS",
    "stage": "Shipped",
}


class FakeHtmlAdapter(MailAdapter):
    """No network — a canned `fetch_html_body` return, mirroring the real
    IMAP/JMAP adapters' §S1 contract `fetch_html_body(uid, folder=None) -> str | None`.
    The other four `MailAdapter` abstract methods are stubbed minimally since
    `mail-extract` never calls them."""

    def __init__(self, account, html):
        self.account = account
        self.source_tag = "[FM]"
        self._html = html

    def capabilities(self):
        return set()

    def search(self, query, folder=None, limit=None):
        return []

    def fetch_message(self, uid, folder=None):
        raise KeyError(uid)

    def list_folders(self):
        return ["INBOX"]

    def fetch_html_body(self, uid, folder=None):
        return self._html


@pytest.fixture(autouse=True)
def restore_cli_fmt():
    """`cmd_mail_extract` reads the module-global `cli._FMT` like every other
    `cmd_*` in `_cli.py` — tests below mutate it directly, so restore it after."""
    original = getattr(cli, "_FMT", "toon")
    yield
    cli._FMT = original


def _client_with(html, account="fastmail_main"):
    adapter = FakeHtmlAdapter(account, html)
    return MailClient({account: adapter})


def test_mail_extract_toon_envelope_carries_count_results_and_next(monkeypatch, capsys):
    client = _client_with(ORDER_JSONLD_HTML)
    monkeypatch.setattr(cli, "build_client", lambda **kw: client)
    cli._FMT = "toon"

    cli.cmd_mail_extract(Namespace(account="fastmail_main", uid="uid-1"))

    out = capsys.readouterr().out
    assert "count" in out, f"TOON envelope must carry count; got {out!r}"
    assert "results[" in out, f"TOON envelope must carry results[]; got {out!r}"
    assert "next" in out, f"TOON envelope must carry next[]; got {out!r}"


def test_mail_extract_order_markup_yields_exact_orders_candidate_and_runnable_add_command(monkeypatch, capsys):
    client = _client_with(ORDER_JSONLD_HTML)
    monkeypatch.setattr(cli, "build_client", lambda **kw: client)
    cli._FMT = "toon"

    cli.cmd_mail_extract(Namespace(account="fastmail_main", uid="uid-1"))

    from vidushi_oa import toon as oa_toon
    payload = oa_toon.from_toon(capsys.readouterr().out)

    assert payload["count"] == 1, f"expected exactly 1 candidate, got {payload}"
    assert len(payload["results"]) == 1
    row = payload["results"][0]
    assert row["type"] == "orders"
    assert row["candidate"] == EXPECTED_ORDER_CANDIDATE, (
        f"orders candidate must carry the exact parsed fields; got {row['candidate']}")

    expected_next_cmd = f"voa add orders --json '{json.dumps(row['candidate'])}'"
    assert expected_next_cmd in payload["next"], (
        f"next[] must contain the exact runnable add command built from the "
        f"candidate; expected {expected_next_cmd!r}, got {payload['next']}")


def test_mail_extract_json_mode_yields_bare_candidates_array(monkeypatch, capsys):
    client = _client_with(ORDER_JSONLD_HTML)
    monkeypatch.setattr(cli, "build_client", lambda **kw: client)
    cli._FMT = "json"

    cli.cmd_mail_extract(Namespace(account="fastmail_main", uid="uid-1"))

    raw = capsys.readouterr().out.strip()
    payload = json.loads(raw)
    assert isinstance(payload, list), f"--json must yield a bare array, got {type(payload)}: {raw!r}"
    assert payload == [{"type": "orders", "candidate": EXPECTED_ORDER_CANDIDATE}]
    assert "next" not in raw
    assert "count" not in raw


def test_mail_extract_no_markup_returns_the_definitive_empty_state_not_an_error(monkeypatch, capsys):
    client = _client_with(PLAIN_HTML)
    monkeypatch.setattr(cli, "build_client", lambda **kw: client)
    cli._FMT = "toon"

    # Must NOT raise SystemExit — the empty state is exit 0, never an error.
    cli.cmd_mail_extract(Namespace(account="fastmail_main", uid="uid-2"))

    from vidushi_oa import toon as oa_toon
    captured = capsys.readouterr()
    assert "error" not in captured.out
    payload = oa_toon.from_toon(captured.out)
    assert payload["count"] == 0
    assert payload["results"] == []
    assert isinstance(payload["next"], list) and payload["next"], (
        "the empty state must still offer a fallback hint so the skill knows to "
        f"fall back to heuristic extraction; got next={payload['next']}")
    assert any("heuristic" in entry.lower() for entry in payload["next"]), (
        f"empty-state next[] must hint at the heuristic-extraction fallback; got {payload['next']}")


def test_mail_extract_is_read_only_and_never_touches_the_backend(monkeypatch, capsys):
    client = _client_with(ORDER_JSONLD_HTML)
    monkeypatch.setattr(cli, "build_client", lambda **kw: client)
    get_backend_mock = mock.Mock()
    monkeypatch.setattr("vidushi_oa.backends.get_backend", get_backend_mock)
    cli._FMT = "json"

    cli.cmd_mail_extract(Namespace(account="fastmail_main", uid="uid-1"))

    get_backend_mock.assert_not_called()


def test_mail_extract_never_surfaces_the_raw_html_body(monkeypatch, capsys):
    client = _client_with(ORDER_JSONLD_HTML)
    monkeypatch.setattr(cli, "build_client", lambda **kw: client)
    cli._FMT = "toon"

    cli.cmd_mail_extract(Namespace(account="fastmail_main", uid="uid-1"))
    toon_out = capsys.readouterr().out
    assert RAW_BODY_SENTINEL not in toon_out, "raw HTML body text leaked into TOON output"
    assert ORDER_JSONLD_HTML not in toon_out, "the full raw HTML body must never be echoed"

    cli._FMT = "json"
    cli.cmd_mail_extract(Namespace(account="fastmail_main", uid="uid-1"))
    json_out = capsys.readouterr().out
    assert RAW_BODY_SENTINEL not in json_out, "raw HTML body text leaked into --json output"
    assert ORDER_JSONLD_HTML not in json_out, "the full raw HTML body must never be echoed"


def test_mail_extract_unknown_account_is_a_structured_error_exit_1(monkeypatch, capsys):
    client = _client_with(ORDER_JSONLD_HTML, account="fastmail_main")
    monkeypatch.setattr(cli, "build_client", lambda **kw: client)
    cli._FMT = "json"

    with pytest.raises(SystemExit) as exc_info:
        cli.cmd_mail_extract(Namespace(account="no_such_account", uid="uid-1"))

    assert exc_info.value.code != 0
    captured = capsys.readouterr()
    assert "Traceback" not in captured.out and "Traceback" not in captured.err
    payload = json.loads(captured.out.strip())
    assert "error" in payload
    assert payload.get("account") == "no_such_account"


def test_help_lists_mail_extract():
    result = subprocess.run([sys.executable, STORE, "--help"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "mail-extract" in result.stdout, f"--help must list mail-extract; got:\n{result.stdout}"


def test_mail_extract_is_wired_via_a_non_test_set_defaults_caller():
    with open(CLI_SRC, encoding="utf-8") as f:
        src = f.read()
    assert src.count("cmd_mail_extract") >= 2, (
        "cmd_mail_extract must be both defined and wired via a set_defaults caller "
        f"in vidushi_oa/_cli.py (found {src.count('cmd_mail_extract')} reference(s))"
    )


if __name__ == "__main__":
    import pytest as _pytest
    raise SystemExit(_pytest.main([__file__]))
