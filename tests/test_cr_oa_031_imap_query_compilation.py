"""CR-OA-031 §S4 — Yahoo / plain IMAP compilation from the portable query
model, RFC 3501 `SEARCH` keys, capability-honest refusal (RED).

Today `ImapAdapter.search()` (`vidushi_oa/mail/imap.py` ~line 159) and its
`YahooImapAdapter.search()` override (~line 571, an identical copy) both do:

    typ, data = conn.uid("SEARCH", query)

-- the RAW portable query string handed straight through as a single RFC 3501
`SEARCH` key. A plain IMAP server has no `subject:`/`newer_than:`/`has:` syntax
of its own, so `subject:X` is meaningless to it (it searches for the literal
text `subject:X` nowhere, matching nothing) and `newer_than:7d` is a silent
no-op -- exactly the defect this CR removes. §S4 fixes this by routing
`search()` through `vidushi_oa.mail.query.parse()` and a compiler that walks
the resulting `QueryModel` tree and emits genuine RFC 3501 `SEARCH` keys
(`SUBJECT`, `FROM`, `TO`, `TEXT`, `SINCE`), refusing (raising) rather than
dropping any qualifier RFC 3501 cannot express (`category:`, `has:attachment`)
-- because silently ignoring them is what produced the wrong answers.

Tests drive the *public* `YahooImapAdapter.search(query)` end-to-end against a
fake IMAP connection (the same `FakeIMAP` shape as
`tests/test_cr_oa_031_gmail_query_compilation.py` and
`tests/test_cr_oa_030_jmap_read_path.py`), capture the exact args handed to
`conn.uid("SEARCH", *args)`, and join them with a single space so the
assertions below are agnostic to whether the eventual compiler emits one
joined string or discrete positional args -- either way the RFC 3501 key
sequence must appear in order.

`newer_than:` cases use the REAL current date (there is no `today=` injection
point on the public `search(query)` API, mirroring
`GmailImapAdapter.search()`, which also computes `reference = date.today()`
internally) -- the expected `SINCE` cutoff is computed the same way the parser
resolves it (`today - N days`), so the test stays correct regardless of which
day it runs.
"""
import unittest
from datetime import date, timedelta

from vidushi_oa.mail.imap import YahooImapAdapter

_MONTH_ABBR = {
    1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
    7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec",
}


def _rfc3501_date(value: date) -> str:
    """Render `value` in RFC 3501's `SEARCH SINCE` date form (`dd-Mon-yyyy`,
    always the English month abbreviation, never locale-dependent)."""
    return f"{value.day:02d}-{_MONTH_ABBR[value.month]}-{value.year}"


class FakeIMAP:
    """Records every `.uid(cmd, *args)` call and returns a canned SEARCH
    response; FETCH is irrelevant to these compilation tests, and an empty
    SEARCH result means `_fetch` never issues a FETCH at all."""

    def __init__(self, search_response=None, fetch_response=None):
        self.search_response = search_response if search_response is not None else ("OK", [b""])
        self.fetch_response = fetch_response if fetch_response is not None else ("OK", [])
        self.login_calls = []
        self.select_calls = []
        self.uid_calls = []

    def login(self, user, password):
        self.login_calls.append((user, password))
        return ("OK", [b"Logged in"])

    def select(self, mailbox="INBOX", readonly=False):
        self.select_calls.append(mailbox)
        return ("OK", [b"1"])

    def uid(self, command, *args):
        self.uid_calls.append((command, args))
        if command.upper() == "SEARCH":
            return self.search_response
        if command.upper() == "FETCH":
            return self.fetch_response
        return ("OK", [None])


def _make_conn_factory(fake):
    calls = []

    def factory(host, port):
        calls.append((host, port))
        return fake

    return factory, calls


def _yahoo_search_args(query: str) -> str:
    """Run `YahooImapAdapter.search(query)` against a fresh `FakeIMAP` and
    return the exact `conn.uid("SEARCH", *args)` arguments joined with a
    single space, in order -- e.g. `'SUBJECT "X" SINCE 23-Jul-2026'`."""
    fake = FakeIMAP()
    factory, _ = _make_conn_factory(fake)
    adapter = YahooImapAdapter(
        account="yahoo_main",
        source_tag="[YH]",
        host="imap.mail.yahoo.com",
        user="me@yahoo.com",
        password="app-pw",
        conn_factory=factory,
    )
    adapter.search(query)
    search_calls = [c for c in fake.uid_calls if c[0].upper() == "SEARCH"]
    assert len(search_calls) == 1, f"expected exactly one SEARCH call, got {search_calls!r}"
    _, args = search_calls[0]
    return " ".join(str(a) for a in args)


class SubjectAndNewerThanCompilationTest(unittest.TestCase):
    """§S4 AC1: `subject:X newer_than:7d` compiles to an RFC 3501 key
    sequence containing `SUBJECT "X"` and `SINCE <dd-Mon-yyyy>` (the resolved
    absolute cutoff). Fails today: `search()` sends the literal lower-case
    portable token string `subject:X newer_than:7d` straight through as one
    opaque SEARCH key, which contains neither `SUBJECT "X"` nor any `SINCE`
    key at all."""

    def test_subject_and_newer_than_compile_to_subject_and_since_keys(self):
        today = date.today()
        expected_since = _rfc3501_date(today - timedelta(days=7))

        joined = _yahoo_search_args("subject:X newer_than:7d")

        self.assertIn(
            'SUBJECT "X"', joined,
            "subject: must compile to the RFC 3501 SUBJECT key with its "
            "value quoted",
        )
        self.assertIn(
            f"SINCE {expected_since}", joined,
            "newer_than:7d must compile to the RFC 3501 SINCE key carrying "
            "the resolved absolute cutoff date in dd-Mon-yyyy form, not the "
            "raw '7d' token",
        )
        self.assertEqual(
            joined, f'SUBJECT "X" SINCE {expected_since}',
            "the full emitted key sequence must be exactly these two keys, "
            "in query order, with no leftover raw qualifier syntax",
        )


class FromToTermAndPhraseCompilationTest(unittest.TestCase):
    """§S4 AC (mapping): `from:`/`to:` map to RFC 3501 `FROM`/`TO`; a bare
    term maps to `TEXT`; a quoted phrase stays ONE `TEXT` value rather than
    splitting on its embedded spaces. Fails today: the raw query string
    (lower-case qualifiers, unquoted phrase) is sent verbatim."""

    def test_from_to_bare_term_and_quoted_phrase_map_to_native_keys(self):
        query = 'from:vendor@example.com to:me@example.com bareterm "quoted phrase"'

        joined = _yahoo_search_args(query)

        self.assertEqual(
            joined,
            'FROM "vendor@example.com" TO "me@example.com" TEXT "bareterm" '
            'TEXT "quoted phrase"',
            "from:/to: must map to RFC 3501 FROM/TO, the bare term to TEXT, "
            "and the quoted phrase must survive as ONE TEXT value rather "
            "than being split on its embedded space",
        )


class GroupOrCompilationTest(unittest.TestCase):
    """§S4 AC (groups): RFC 3501 has no parentheses for SEARCH keys -- `OR` is
    a prefix, BINARY keyword, so a group compiles to nested `OR key1 key2`
    forms, and a 3-way OR nests as `OR key1 (OR key2 key3)` -- rendered here
    as `OR key1 OR key2 key3` (RFC 3501 has no parentheses to make the
    nesting visually explicit; the second `OR` IS the nested group). Fails
    today: the raw query string, parentheses and all, is sent through
    verbatim -- a real IMAP server has no `(`/`)` SEARCH syntax at all."""

    def test_two_way_or_group_compiles_to_prefix_or_form(self):
        joined = _yahoo_search_args("(a OR b) c")

        self.assertEqual(
            joined, 'OR TEXT "a" TEXT "b" TEXT "c"',
            "a 2-way OR group must compile to RFC 3501's prefix-OR form "
            "(OR key1 key2), followed by the implicit-AND term outside the "
            "group",
        )

    def test_three_way_or_group_compiles_to_nested_prefix_or_form(self):
        joined = _yahoo_search_args("(a OR b OR c) d")

        self.assertEqual(
            joined, 'OR TEXT "a" OR TEXT "b" TEXT "c" TEXT "d"',
            "a 3-way OR group must nest as OR key1 (OR key2 key3), since "
            "RFC 3501's OR is strictly binary -- rendered without "
            "parentheses as 'OR TEXT \"a\" OR TEXT \"b\" TEXT \"c\"' -- "
            "followed by the implicit-AND term outside the group",
        )


class UnsupportedQualifierRefusalTest(unittest.TestCase):
    """§S4 AC2: a qualifier with no RFC 3501 equivalent (`has:attachment`,
    `category:`) is REFUSED (raises), never silently dropped -- silently
    ignoring them on a plain IMAP server is exactly what produced the wrong
    answers this CR exists to remove. (The structured-error/exit-code
    envelope is §S5's job; this only pins that the adapter refuses rather
    than returning a result.) Fails today: `search()` sends the raw query
    straight through and returns normally -- no exception is raised for
    either qualifier, so the search silently "succeeds" with whatever (wrong)
    result the plain-text SEARCH key happens to match."""

    def test_has_attachment_against_plain_imap_is_refused_not_dropped(self):
        with self.assertRaises(Exception) as ctx:
            _yahoo_search_args("has:attachment")

        self.assertIn(
            "has:attachment", str(ctx.exception),
            "the refusal must name the unsupported qualifier so the caller "
            "knows exactly what was rejected",
        )

    def test_category_against_plain_imap_is_refused_not_dropped(self):
        with self.assertRaises(Exception) as ctx:
            _yahoo_search_args("category:purchases")

        self.assertIn(
            "category", str(ctx.exception),
            "the refusal must name the unsupported qualifier so the caller "
            "knows exactly what was rejected",
        )


if __name__ == "__main__":
    unittest.main()
