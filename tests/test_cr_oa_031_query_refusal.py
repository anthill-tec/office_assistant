"""CR-OA-031 §S5 — unsupported qualifiers are a structured error, never a
silent empty (RED).

Prior cycles are already GREEN on this branch: `vidushi_oa/mail/query.py`
(`parse` -> `QueryModel.root` tree, `QueryParseError`), `jmap.compile_filter`,
`imap.compile_gmail_query`, `imap.compile_imap_query` +
`UnsupportedQualifierError` (raised for `has:attachment`/`category:` on plain
RFC 3501 IMAP). What §S5 adds is the USER-FACING contract: a qualifier the
target provider cannot express -- or an unparseable/unknown qualifier or unit
-- must surface as an AXI #6 structured error (never `count: 0`), and in a
multi-account search must degrade into the existing `failed_accounts[]`
fail-soft envelope rather than failing the whole search.

Ground truth checked against this branch before writing these tests (see the
RED-agent report): `MailClient.search()` already isolates ANY exception an
adapter's `search()` raises (CR-OA-020's generic per-adapter fail-soft), and
`cmd_mail_search` already renders a total wipeout as `{"error": ...,
"failed_accounts": [...]}` + exit 1. Because `compile_imap_query` already
raises `UnsupportedQualifierError` for `has:attachment`/`category:`, and
`parse()` already raises `QueryParseError` for an unknown qualifier/unit, the
PLAIN-IMAP scenarios in AC1/AC2 below already pass through that existing
machinery -- pinned here as regression guards, called out explicitly rather
than contrived into failing.

The genuine gap is JMAP: `compile_filter`/`_compile_node` (vidushi_oa/mail/jmap.py)
has NO branch for `category:` at all, so it silently compiles to an EMPTY `{}`
filter instead of refusing -- the exact "silently wrong answer" this CR exists
to remove, and never once mentions `UnsupportedQualifierError`. The JMAP tests
below, and the mechanical audit, target that gap and are expected to FAIL.

Also pinned here (CARRY-IN, §S1): quoted-colon tokenizing. A colon INSIDE a
quoted qualifier value is already literal (`subject:"re: invoice"` already
resolves correctly -- pinned as a regression guard, noted explicitly). A
standalone quoted colon-bearing token (`"https://example.com/x"`) should land
as a plain term but today still raises `QueryParseError`, because `_leaf`
partitions on the first `:` survives quote-stripping and treats `https` as an
unknown qualifier name. An UNQUOTED colon-bearing token must keep raising, but
its message must hint that quoting makes it literal -- which it does not
today.
"""
import inspect
import json
import re
import unittest
from argparse import Namespace

import pytest

import vidushi_oa._cli as cli
from vidushi_oa import toon as oa_toon
from vidushi_oa.mail import jmap as jmap_module
from vidushi_oa.mail.client import MailClient
from vidushi_oa.mail.imap import GmailImapAdapter, ImapAdapter, UnsupportedQualifierError
from vidushi_oa.mail.jmap import JmapAdapter, compile_filter
from vidushi_oa.mail.query import QueryParseError, parse

_JMAP_SESSION_URL = "https://api.fastmail.com/jmap/session"
_JMAP_API_URL = "https://api.fastmail.com/jmap/api/"
_JMAP_ACCOUNT_ID = "u1234567"

_CANNED_JMAP_SESSION = {
    "apiUrl": _JMAP_API_URL,
    "accounts": {
        _JMAP_ACCOUNT_ID: {
            "name": "you@fastmail.com",
            "isPersonal": True,
            "accountCapabilities": {"urn:ietf:params:jmap:mail": {}},
        },
    },
    "primaryAccounts": {"urn:ietf:params:jmap:mail": _JMAP_ACCOUNT_ID},
    "capabilities": {"urn:ietf:params:jmap:mail": {}},
}

_EMPTY_EMAIL_GET_RESPONSE = {
    "methodResponses": [
        ["Email/query", {"accountId": _JMAP_ACCOUNT_ID, "ids": []}, "0"],
        ["Email/get", {"accountId": _JMAP_ACCOUNT_ID, "list": [], "notFound": []}, "1"],
    ],
}


class _FakeJmapTransport:
    """Records every `(method, url, headers, body)` call and answers canned
    `(status, dict)` tuples -- no network. Matches the seam
    `tests/test_cr_oa_030_jmap_read_path.py`'s `FakeTransport` already uses."""

    def __init__(self, post_response=None):
        self.post_response = post_response or _EMPTY_EMAIL_GET_RESPONSE
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        if method == "GET":
            return 200, _CANNED_JMAP_SESSION
        return 200, self.post_response

    def calls_of(self, method):
        return [c for c in self.calls if c[0] == method]


class _FakePlainImapConn:
    """A plain (non-Gmail) IMAP connection whose `.uid()` must NEVER be
    reached: an unsupported/unparseable qualifier must be refused entirely
    client-side, before any SEARCH is issued on the wire."""

    def login(self, user, password):
        return ("OK", [b"Logged in"])

    def select(self, mailbox="INBOX", readonly=False):
        return ("OK", [b"1"])

    def uid(self, *args, **kwargs):
        raise AssertionError(
            "must not reach the wire: an unsupported/unparseable qualifier "
            "must be refused before any IMAP SEARCH is issued")


class _FakeGmailConn:
    """A Gmail IMAP connection that answers ONE message for any `X-GM-RAW`
    SEARCH -- Gmail natively supports `category:`, so this account must serve
    the query the plain-IMAP sibling account refuses."""

    _HEADER_BYTES = (
        b"Subject: Receipt\r\nFrom: shop@example.com\r\nTo: me@example.com\r\n"
        b"Date: Mon, 20 Jul 2026 10:00:00 +0000\r\nMessage-ID: <gm-1@example.com>\r\n\r\n"
    )

    def login(self, user, password):
        return ("OK", [b"Logged in"])

    def select(self, mailbox="INBOX", readonly=False):
        return ("OK", [b"1"])

    def uid(self, command, *args):
        if command.upper() == "SEARCH":
            return ("OK", [b"1"])
        if command.upper() == "FETCH":
            descriptor = (
                f"1 (UID 5 X-GM-THRID 9 BODY[HEADER.FIELDS (SUBJECT FROM TO "
                f"DATE MESSAGE-ID REFERENCES IN-REPLY-TO)] "
                f"{{{len(self._HEADER_BYTES)}}}"
            ).encode()
            return ("OK", [(descriptor, self._HEADER_BYTES), b")"])
        return ("OK", [None])


def _plain_imap_client():
    adapter = ImapAdapter(
        account="yahoo_main", source_tag="[YH]", host="imap.mail.yahoo.com",
        user="me@example.com", password="app-pw",
        conn_factory=lambda host, port: _FakePlainImapConn(),
    )
    return MailClient({"yahoo_main": adapter})


def _jmap_client(post_response=None):
    transport = _FakeJmapTransport(post_response=post_response)
    adapter = JmapAdapter(
        account="fastmail_main", source_tag="[FM]", token="secret-token",
        session_url=_JMAP_SESSION_URL, transport=transport,
    )
    return MailClient({"fastmail_main": adapter}), transport


def _gmail_plus_yahoo_client():
    gmail = GmailImapAdapter(
        account="gmail_main", source_tag="[GM]", host="imap.gmail.com",
        user="me@gmail.com", password="app-pw",
        conn_factory=lambda host, port: _FakeGmailConn(),
    )
    yahoo = ImapAdapter(
        account="yahoo_main", source_tag="[YH]", host="imap.mail.yahoo.com",
        user="me@example.com", password="app-pw",
        conn_factory=lambda host, port: _FakePlainImapConn(),
    )
    return MailClient({"gmail_main": gmail, "yahoo_main": yahoo})


@pytest.fixture(autouse=True)
def _restore_cli_fmt_cr_oa_031():
    """`cmd_mail_search` reads the module-global `cli._FMT`; these tests mutate
    it directly, so restore it afterwards (matches the fixture of the same
    purpose in `tests/test_cr_oa_020_mail_verbs.py` /
    `tests/test_cr_oa_030_jmap_read_path.py`)."""
    original = getattr(cli, "_FMT", "toon")
    yield
    cli._FMT = original


# ─────────────────── §S5 AC1 — single-account refusal ───────────────────

@pytest.mark.parametrize("query,offending_token", [
    ("has:attachment", "has:attachment"),
    ("bogus:x", "bogus:x"),
    ("newer_than:3q", "newer_than:3q"),
])
def test_single_plain_imap_account_unsupported_or_unparseable_qualifier_is_structured_error(
        query, offending_token, monkeypatch, capsys):
    """§S5 AC1 (plain-IMAP regression pin -- ALREADY GREEN today via the
    existing CR-OA-020 fail-soft machinery): `has:attachment` (RFC 3501 cannot
    express it), an unknown qualifier (`bogus:x`), and an unknown relative-date
    unit (`newer_than:3q`) against a single plain-IMAP account each exit
    NON-ZERO with a structured error naming the offending token AND the
    account, no traceback, and never a `count` key (never `count: 0`)."""
    client = _plain_imap_client()
    monkeypatch.setattr(cli, "build_client", lambda **kw: client)
    cli._FMT = "json"

    with pytest.raises(SystemExit) as exc_info:
        cli.cmd_mail_search(Namespace(query=query, accounts=["yahoo_main"]))

    assert exc_info.value.code != 0
    captured = capsys.readouterr()
    assert "Traceback" not in captured.out and "Traceback" not in captured.err
    payload = json.loads(captured.out.strip())
    assert "count" not in payload, (
        f"a refused qualifier must never look like a legitimate count, got {payload!r}")
    failed = {f["account"]: f["error"] for f in payload["failed_accounts"]}
    assert list(failed) == ["yahoo_main"]
    assert offending_token in failed["yahoo_main"], (
        f"structured error must name the offending qualifier/token "
        f"{offending_token!r}, got {failed['yahoo_main']!r}")


def test_single_jmap_account_refuses_category_instead_of_silently_matching_everything(
        monkeypatch, capsys):
    """§S5 AC1 (JMAP capability gap -- the genuine RED case): `category:` has
    no JMAP filter equivalent (per `compile_filter`'s own docstring), so a
    JMAP-only search using it must be REFUSED with a structured error naming
    the qualifier and the account -- never silently compiled down to an EMPTY
    `{}` filter that would match every message in the mailbox. Fails today:
    neither `compile_filter` nor `JmapAdapter.search` ever raise for
    `category:`; the request would go out to the wire with an empty filter."""
    client, transport = _jmap_client()
    monkeypatch.setattr(cli, "build_client", lambda **kw: client)
    cli._FMT = "json"

    with pytest.raises(SystemExit) as exc_info:
        cli.cmd_mail_search(Namespace(query="category:purchases", accounts=["fastmail_main"]))

    assert exc_info.value.code != 0
    captured = capsys.readouterr()
    assert "Traceback" not in captured.out and "Traceback" not in captured.err
    payload = json.loads(captured.out.strip())
    assert "count" not in payload, (
        f"a refused qualifier must never look like a legitimate count, got {payload!r}")
    failed = {f["account"]: f["error"] for f in payload["failed_accounts"]}
    assert list(failed) == ["fastmail_main"]
    assert "category" in failed["fastmail_main"], (
        f"structured error must name the offending qualifier 'category:', "
        f"got {failed['fastmail_main']!r}")
    post_calls = transport.calls_of("POST")
    assert not post_calls, (
        "an unsupported qualifier must be refused BEFORE any JMAP request is "
        f"sent -- got {post_calls!r}")


# ─────────────────── §S5 AC2 — multi-account fail-soft ───────────────────

def test_multi_account_one_refuses_qualifier_other_serves_it_fail_soft(monkeypatch, capsys):
    """§S5 AC2 (regression pin -- ALREADY GREEN today via the existing
    CR-OA-020 fail-soft contract): with TWO accounts where plain-IMAP
    (`yahoo_main`, RFC 3501 has no category search key) refuses `category:`
    and Gmail (`gmail_main`, native `category:` operator) can serve it, the
    search returns the Gmail row, lists `yahoo_main` under `failed_accounts[]`
    naming the qualifier, and exits 0 -- never failing the whole search."""
    client = _gmail_plus_yahoo_client()
    monkeypatch.setattr(cli, "build_client", lambda **kw: client)
    cli._FMT = "toon"

    cli.cmd_mail_search(Namespace(query="category:purchases", accounts=None))

    captured = capsys.readouterr()
    assert "Traceback" not in captured.out and "Traceback" not in captured.err
    payload = oa_toon.from_toon(captured.out)
    assert payload["count"] == 1
    assert payload["results"][0]["account"] == "gmail_main"
    failed = {f["account"]: f["error"] for f in payload["failed_accounts"]}
    assert list(failed) == ["yahoo_main"]
    assert "category" in failed["yahoo_main"]


# ─────────────────── §S5 AC3 — mechanically auditable no-silent-empty ───────────────────

class NoSilentEmptyForUnsupportedQualifierTest(unittest.TestCase):
    """§S5 AC3: a grep shows no adapter `search` path in `vidushi_oa/mail/`
    returns an empty/pass-through result for an unsupported/unparseable
    qualifier -- every such path must RAISE. `vidushi_oa/mail/imap.py`
    (`_render_imap_node`) already raises `UnsupportedQualifierError` for
    `category:`/`has:attachment`; `vidushi_oa/mail/jmap.py` does not, for
    `category:`. Modeled on the existing grep-style guard
    `tests.test_cr_oa_030_jmap_read_path.SharedMethodErrorCheckIsNotDuplicatedTest`.
    """

    def test_jmap_module_source_never_mentions_unsupportedqualifiererror(self):
        source = inspect.getsource(jmap_module)
        hits = re.findall(r"UnsupportedQualifierError", source)
        self.assertGreaterEqual(
            len(hits), 1,
            "vidushi_oa/mail/jmap.py never references UnsupportedQualifierError: "
            "category: (no JMAP filter equivalent, per compile_filter's own "
            "docstring) is silently dropped by _compile_node instead of being "
            "refused -- exactly the silent-empty defect this CR removes")

    def test_compile_filter_raises_for_category_rather_than_compiling_to_an_empty_filter(self):
        model = parse("category:purchases")

        with self.assertRaises(UnsupportedQualifierError) as ctx:
            compile_filter(model)

        self.assertIn("category", str(ctx.exception))


# ─────────────────── §S5 carry-in — quoted-colon edges (§S1) ───────────────────

class QuotedColonEdgeCasesTest(unittest.TestCase):
    """Carry-in pinned here per the CR-OA-031 §S5 dispatch: a colon INSIDE a
    quoted qualifier value or a standalone quoted phrase must stay literal,
    never re-parsed as a qualifier prefix; a colon in an UNQUOTED token must
    still raise `QueryParseError`, but its message must hint that quoting
    makes it literal."""

    def test_quoted_qualifier_value_containing_a_colon_stays_literal(self):
        # ALREADY GREEN today: `_tokenize` strips the surrounding quotes before
        # the qualifier check runs, and `_leaf` partitions on the FIRST colon
        # only, so the phrase's OWN internal colon survives untouched in the
        # qualifier's value rather than being re-parsed.
        model = parse('subject:"re: invoice"')
        self.assertEqual(model.subject, "re: invoice")

    def test_standalone_quoted_colon_bearing_phrase_lands_as_a_term(self):
        # Fails today: the tokenizer strips the phrase's quotes before `_leaf`
        # sees it, so `"https://example.com/x"` arrives at `_leaf` as the bare
        # string `https://example.com/x` -- indistinguishable from an unquoted
        # token -- and `_leaf` partitions on its `:` and rejects `https` as an
        # unknown qualifier name instead of returning it as a term.
        model = parse('"https://example.com/x"')
        self.assertEqual(model.terms, ["https://example.com/x"])

    def test_unquoted_colon_bearing_token_still_raises_naming_the_token(self):
        with self.assertRaises(QueryParseError) as ctx:
            parse("https://example.com/x")
        self.assertIn("https://example.com/x", str(ctx.exception))

    def test_unquoted_colon_bearing_token_error_hints_that_quoting_makes_it_literal(self):
        # Fails today: the message enumerates the known qualifiers but gives
        # no hint that wrapping the token in quotes avoids the rejection.
        with self.assertRaises(QueryParseError) as ctx:
            parse("https://example.com/x")
        message = str(ctx.exception).lower()
        self.assertTrue(
            "quote" in message or "quoting" in message,
            f"error message must hint that quoting the token makes it "
            f"literal, got {message!r}")

    def test_unquoted_re_colon_invoice_raises_naming_the_token_with_the_same_hint(self):
        with self.assertRaises(QueryParseError) as ctx:
            parse("re:invoice")
        message = str(ctx.exception)
        self.assertIn("re:invoice", message)
        self.assertTrue(
            "quote" in message.lower() or "quoting" in message.lower(),
            f"error message must hint that quoting makes 're:invoice' "
            f"literal, got {message!r}")


if __name__ == "__main__":
    unittest.main()
