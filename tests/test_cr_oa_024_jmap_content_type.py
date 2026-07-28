"""CR-OA-024 §S1 — Fastmail JMAP POST missing `Content-Type` header (RED).

`JmapAdapter._auth_headers()` (`vidushi_oa/mail/jmap.py:71-72`) currently returns
only `{"Authorization": f"Bearer {self.token}"}`. Fastmail's JMAP API 400s the POST
(`jmap.py:103`) because the request lacks `Content-Type: application/json`. This
file asserts:

  1. `_auth_headers()` returns a dict whose entries are EXACTLY
     `Authorization: Bearer <token>` and `Content-Type: application/json`.
  2. Driving `JmapAdapter` through its real public `search()` path (via an
     injected fake transport, no network) captures the headers passed to the
     `POST` at the JMAP `api_url`, and those headers include
     `Content-Type: application/json`.

Both must FAIL against current code because `_auth_headers()` omits the
`Content-Type` key entirely.

§S2 (below, `test_mail_search_jmap_non_200_account_fails_soft_and_keeps_healthy_fm_rows`)
is a CHARACTERIZATION / regression-guard test, not a RED-for-new-behavior test: it
drives `mail-search` end-to-end (through `cli.cmd_mail_search`, like
`tests/test_cr_oa_020_mail_verbs.py`) with two fake JMAP accounts — one whose POST
returns a non-200 (so `JmapAdapter.search()` raises `RuntimeError`, per `jmap.py`
lines ~107-108) and one healthy account — and asserts the existing fail-soft
machinery in `MailClient.search()` (`vidushi_oa/mail/client.py`) and
`cmd_mail_search` (`vidushi_oa/_cli.py`) already produces an AXI-conformant
result: the failing account lands in `failed_accounts` (no traceback), the
healthy account's row is still returned, and the envelope stays the standard
`{count, results, next}` shape. This is EXPECTED TO PASS on current code — it
pins down that the §S1 `Content-Type` fix didn't need any additional fail-soft
wiring, and guards against a future regression in that wiring.
"""
import unittest
from argparse import Namespace

import pytest

import vidushi_oa._cli as cli
from vidushi_oa.mail.client import MailClient
from vidushi_oa.mail.jmap import JmapAdapter

SESSION_URL = "https://api.fastmail.com/jmap/session"
API_URL = "https://api.fastmail.com/jmap/api/"
ACCOUNT_ID = "u1234567"

_CANNED_SESSION = {
    "apiUrl": API_URL,
    "accounts": {
        ACCOUNT_ID: {
            "name": "new.book1604@fastmail.com",
            "isPersonal": True,
            "accountCapabilities": {"urn:ietf:params:jmap:mail": {}},
        },
    },
    "primaryAccounts": {"urn:ietf:params:jmap:mail": ACCOUNT_ID},
    "capabilities": {"urn:ietf:params:jmap:mail": {}},
}

_CANNED_POST_RESPONSE = {
    "methodResponses": [
        ["Email/query", {"accountId": ACCOUNT_ID, "ids": []}, "0"],
        [
            "Email/get",
            {"accountId": ACCOUNT_ID, "list": [], "notFound": []},
            "1",
        ],
    ],
}


class FakeTransport:
    """Records every `(method, url, headers, body)` call and returns canned
    `(status, dict)` tuples shaped like real JMAP HTTP responses — no network."""

    def __init__(self, session_response=None, post_response=None):
        self.session_response = session_response or _CANNED_SESSION
        self.post_response = post_response or _CANNED_POST_RESPONSE
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        if method == "GET":
            return 200, self.session_response
        return 200, self.post_response

    def calls_of(self, method):
        return [c for c in self.calls if c[0] == method]


class JmapAuthHeadersContentTypeTest(unittest.TestCase):
    """§S1 AC: `_auth_headers()` returns exactly Authorization + Content-Type."""

    def test_auth_headers_includes_authorization_and_json_content_type(self):
        adapter = JmapAdapter(
            account="fastmail_main", source_tag="[FM]", token="secret-token",
            session_url=SESSION_URL, transport=FakeTransport(),
        )

        headers = adapter._auth_headers()

        self.assertEqual(
            headers,
            {
                "Authorization": "Bearer secret-token",
                "Content-Type": "application/json",
            },
        )
        self.assertEqual(headers["Authorization"], "Bearer secret-token")
        self.assertEqual(headers["Content-Type"], "application/json")


class JmapSearchPostContentTypeIntegrationTest(unittest.TestCase):
    """§S1 AC (integration): the real `search()` POST path must carry the
    Content-Type header — driven through the public API, not `_auth_headers()`
    called directly."""

    def test_search_post_to_api_url_carries_json_content_type_header(self):
        transport = FakeTransport()
        adapter = JmapAdapter(
            account="fastmail_main", source_tag="[FM]", token="secret-token",
            session_url=SESSION_URL, transport=transport,
        )

        adapter.search("invoice")

        post_calls = transport.calls_of("POST")
        self.assertEqual(len(post_calls), 1)
        method, url, headers, body = post_calls[0]
        self.assertEqual(url, API_URL)
        self.assertEqual(headers.get("Content-Type"), "application/json")
        self.assertEqual(headers.get("Authorization"), "Bearer secret-token")


_HEALTHY_POST_RESPONSE = {
    "methodResponses": [
        ["Email/query", {"accountId": ACCOUNT_ID, "ids": ["abc"]}, "0"],
        [
            "Email/get",
            {
                "accountId": ACCOUNT_ID,
                "list": [
                    {
                        "id": "abc",
                        "threadId": "t1",
                        "messageId": ["<healthy-1@fastmail.com>"],
                        "subject": "Healthy JMAP message",
                        "from": [{"name": "Sender", "email": "sender@fastmail.com"}],
                        "to": [{"email": "new.book1604@fastmail.com"}],
                        "receivedAt": "2026-07-20T10:00:00Z",
                        "deliveredTo": "new.book1604@fastmail.com",
                    },
                ],
                "notFound": [],
            },
            "1",
        ],
    ],
}


class _NonTwoHundredTransport:
    """Fake transport for a Fastmail-style JMAP account whose POST to the JMAP
    `api_url` returns a non-200 (Fastmail 400ing an unauthorized/malformed
    request). The GET session fetch still succeeds so the failure is isolated to
    the search POST itself: `JmapAdapter.search()` raises `RuntimeError` from
    `jmap.py`'s `if status != 200: raise RuntimeError(...)`, which
    `MailClient.search()`'s per-adapter `except Exception` must catch."""

    def __init__(self, session_response=None, post_status=400):
        self.session_response = session_response or _CANNED_SESSION
        self.post_status = post_status
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        if method == "GET":
            return 200, self.session_response
        return self.post_status, {"type": "unauthorized", "status": self.post_status}


@pytest.fixture(autouse=True)
def _restore_cli_fmt():
    """`cmd_mail_search` reads the module-global `cli._FMT` — restore it after the
    §S2 test below mutates it, matching `tests/test_cr_oa_020_mail_verbs.py`."""
    original = getattr(cli, "_FMT", "toon")
    yield
    cli._FMT = original


def test_mail_search_jmap_non_200_account_fails_soft_and_keeps_healthy_fm_rows(monkeypatch, capsys):
    """§S2 characterization guard: a Fastmail-style JMAP account whose search POST
    404/400s must NOT blank the whole `mail-search` fan-out. Driven through the
    real `cli.cmd_mail_search` path (like `test_cr_oa_020_mail_verbs.py`), with a
    broken JMAP account (non-200 POST) and a healthy JMAP account registered on
    the same `MailClient`."""
    healthy_transport = FakeTransport(post_response=_HEALTHY_POST_RESPONSE)
    healthy = JmapAdapter(
        account="fastmail_healthy", source_tag="[FM]", token="secret-token",
        session_url=SESSION_URL, transport=healthy_transport,
    )
    broken_transport = _NonTwoHundredTransport(post_status=400)
    broken = JmapAdapter(
        account="fastmail_broken", source_tag="[FM]", token="secret-token",
        session_url=SESSION_URL, transport=broken_transport,
    )

    client = MailClient({"fastmail_broken": broken, "fastmail_healthy": healthy})
    monkeypatch.setattr(cli, "build_client", lambda **kw: client)
    cli._FMT = "toon"

    cli.cmd_mail_search(Namespace(query="invoice", accounts=None))

    from vidushi_oa import toon as oa_toon
    captured = capsys.readouterr()
    assert "Traceback" not in captured.out and "Traceback" not in captured.err

    payload = oa_toon.from_toon(captured.out)

    # Standard {count, results, next} TOON envelope, plus failed_accounts for the
    # partial failure -- not a blanked/empty result set.
    assert payload["count"] == 1
    assert isinstance(payload["next"], list) and payload["next"]

    assert len(payload["results"]) == 1
    healthy_row = payload["results"][0]
    assert healthy_row["id"] == "<healthy-1@fastmail.com>"
    assert healthy_row["subject"] == "Healthy JMAP message"
    assert healthy_row["source_tag"] == "[FM]", (
        f"healthy JMAP account's row must carry the [FM] source tag, got {healthy_row}"
    )

    assert payload["tally"] == {"source_tag": {"FM": 1}}

    failed = {row["account"]: row["error"] for row in payload["failed_accounts"]}
    assert list(failed) == ["fastmail_broken"], (
        f"the non-200 JMAP account must be the only failed_accounts entry, got {failed}"
    )
    assert "HTTP 400" in failed["fastmail_broken"]
    assert "Traceback" not in failed["fastmail_broken"]
    assert isinstance(failed["fastmail_broken"], str)


if __name__ == "__main__":
    unittest.main()
