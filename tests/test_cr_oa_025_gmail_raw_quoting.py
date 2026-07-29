"""CR-OA-025 §S1 — Gmail `X-GM-RAW` search must send a valid IMAP argument for a
quoted-phrase query (RED).

`GmailImapAdapter.search()` (`vidushi_oa/mail/imap.py`) currently builds the
`X-GM-RAW` argument with `'"%s"' % query` — it wraps the query in double-quotes
but does not escape embedded `"` or `\\`. A quoted-phrase query (e.g.
`category:purchases "out for delivery"`) therefore becomes an **unbalanced**
IMAP quoted-string, since the query's own embedded `"` characters are not
escaped.

These tests drive the *public* `GmailImapAdapter.search(query)` end-to-end
against a fake IMAP connection (the same `FakeIMAP` shape used by
`test_cr_oa_020_imap_adapters.py`) and assert on the exact `<arg>` captured by
`conn.uid("SEARCH", "X-GM-RAW", <arg>)`:

  1. a quoted-phrase query must yield a well-formed RFC 3501 quoted-string —
     embedded `"` escaped as `\\"` and `\\` escaped as `\\\\` — NOT the naive
     `'"%s"' % query` output (unbalanced). THIS TEST FAILS against current code.
  2. a quote-free compound query must not be over-escaped (regression guard).
  3. a compound query mixing qualifiers/`OR`/parentheses *and* a quoted phrase
     must also translate to a well-formed, round-trippable argument via the
     same public `search()` entry point (integration on the production path).
"""
import unittest

from vidushi_oa.mail.imap import GmailImapAdapter


class FakeIMAP:
    """Records every `.uid(cmd, *args)` call and returns a canned SEARCH
    response; FETCH is irrelevant to these quoting tests but must return an
    empty-but-well-formed payload so `search()` completes without error."""

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


def _unescape_imap_quoted_string(arg):
    """Recover the literal value from an RFC 3501 quoted-string (`"`-delimited,
    `\\`-escaped) IMAP argument, so a test can assert the escaped wire form
    round-trips back to the exact original query. Raises AssertionError with a
    descriptive message if `arg` is not a well-formed quoted-string (e.g. it
    contains a stray, unescaped `"` — the exact defect in the naive output)."""
    if not (isinstance(arg, str) and len(arg) >= 2 and arg.startswith('"') and arg.endswith('"')):
        raise AssertionError(f"not a well-formed IMAP quoted-string: {arg!r}")
    inner = arg[1:-1]
    out = []
    i = 0
    while i < len(inner):
        ch = inner[i]
        if ch == "\\":
            if i + 1 >= len(inner):
                raise AssertionError(f"dangling escape in IMAP quoted-string: {arg!r}")
            out.append(inner[i + 1])
            i += 2
        elif ch == '"':
            raise AssertionError(
                f"unescaped '\"' inside IMAP quoted-string (unbalanced): {arg!r}"
            )
        else:
            out.append(ch)
            i += 1
    return "".join(out)


class GmailRawQuotedPhraseSearchTest(unittest.TestCase):
    """§S1 AC: a quoted-phrase query yields a valid, escaped IMAP quoted-string
    `X-GM-RAW` argument — not the naive unbalanced `'"%s"' % query` output."""

    def setUp(self):
        self.fake = FakeIMAP(search_response=("OK", [b""]))
        self.factory, self.factory_calls = _make_conn_factory(self.fake)
        self.adapter = GmailImapAdapter(
            account="gmail_main",
            source_tag="[GM]",
            host="imap.gmail.com",
            user="me@gmail.com",
            password="app-pw",
            conn_factory=self.factory,
        )

    def test_quoted_phrase_query_produces_well_formed_escaped_quoted_string_arg(self):
        query = 'category:purchases "out for delivery"'

        self.adapter.search(query)

        search_calls = [c for c in self.fake.uid_calls if c[0].upper() == "SEARCH"]
        self.assertEqual(len(search_calls), 1)
        _, args = search_calls[0]
        self.assertEqual(args[0], "X-GM-RAW")
        captured_arg = args[1]

        naive_arg = '"%s"' % query
        self.assertNotEqual(
            captured_arg,
            naive_arg,
            "search() must not send the naive unescaped '\"%s\"' quoting for a "
            "quoted-phrase query -- it produces an unbalanced IMAP quoted-string",
        )

        expected_arg = '"' + query.replace("\\", "\\\\").replace('"', '\\"') + '"'
        self.assertEqual(
            captured_arg,
            expected_arg,
            "X-GM-RAW arg must escape embedded '\"' as '\\\"' (and '\\\\' as "
            "'\\\\\\\\') within the RFC 3501 quoted-string",
        )

        self.assertEqual(
            _unescape_imap_quoted_string(captured_arg),
            query,
            "the escaped IMAP quoted-string must round-trip back to the exact "
            "original query",
        )


class GmailRawQuoteFreeRegressionTest(unittest.TestCase):
    """§S1 AC (regression): a quote-free compound query (qualifiers + no
    quoted phrase) must not be over-escaped -- no stray backslashes are
    introduced and the argument stays a well-formed IMAP quoted-string.

    Reconciled for CR-OA-031 §S3: the ESCAPING contract this test exists to
    pin is unchanged, but the query is no longer passed through raw -- it is
    recompiled from the portable query model, so the portable
    `newer_than:3m` (a calendar-free 30-day month) reaches the wire as
    Gmail's own `newer_than:90d`. What is pinned here is therefore the
    escaping of the COMPILED wire string, not raw round-trip equality (which
    CR-OA-031 deliberately supersedes)."""

    def setUp(self):
        self.fake = FakeIMAP(search_response=("OK", [b""]))
        self.factory, self.factory_calls = _make_conn_factory(self.fake)
        self.adapter = GmailImapAdapter(
            account="gmail_main",
            source_tag="[GM]",
            host="imap.gmail.com",
            user="me@gmail.com",
            password="app-pw",
            conn_factory=self.factory,
        )

    def test_quote_free_compound_query_is_not_over_escaped(self):
        query = "category:purchases newer_than:3m"

        self.adapter.search(query)

        search_calls = [c for c in self.fake.uid_calls if c[0].upper() == "SEARCH"]
        self.assertEqual(len(search_calls), 1)
        _, args = search_calls[0]
        captured_arg = args[1]

        # CR-OA-031 §S3: `newer_than:3m` is COMPILED to Gmail's own day unit
        # (the portable grammar's calendar-free 30-day month -> 90 days), so
        # the wire string is the compiled query, not the raw one.
        compiled = "category:purchases newer_than:90d"
        expected_arg = '"%s"' % compiled
        self.assertEqual(
            captured_arg,
            expected_arg,
            "a quote-free query must produce a well-formed quoted-string "
            "around the COMPILED query -- no stray backslashes introduced",
        )
        self.assertNotIn(
            "\\",
            captured_arg,
            "no backslash-escaping should be introduced for a query with no "
            "embedded '\"' or '\\\\'",
        )
        self.assertEqual(_unescape_imap_quoted_string(captured_arg), compiled)


class GmailRawCompoundQuotedPhraseIntegrationTest(unittest.TestCase):
    """§S1 AC (integration, production path): a compound query mixing
    qualifiers, `OR`, parentheses, AND a quoted phrase must also translate to
    a well-formed `X-GM-RAW` argument via the public `search()` entry point --
    not a private quoting helper exercised in isolation.

    Reconciled for CR-OA-031 §S3: parentheses are now IN-grammar and are
    re-emitted as Gmail's own native parentheses, so the group survives
    compilation; `label:` is not advertised anywhere and stays a non-goal, so
    the second alternative is a grammar-valid `subject:` qualifier. What is
    pinned here is unchanged: the quoted phrase survives as a well-formed,
    correctly escaped IMAP quoted-string over the public `search()` path, on
    exactly one connection."""

    def setUp(self):
        self.fake = FakeIMAP(search_response=("OK", [b""]))
        self.factory, self.factory_calls = _make_conn_factory(self.fake)
        self.adapter = GmailImapAdapter(
            account="gmail_main",
            source_tag="[GM]",
            host="imap.gmail.com",
            user="me@gmail.com",
            password="app-pw",
            conn_factory=self.factory,
        )

    def test_compound_query_with_or_parentheses_and_quoted_phrase_round_trips(self):
        query = '(category:purchases OR subject:orders) "out for delivery"'

        self.adapter.search(query)

        search_calls = [c for c in self.fake.uid_calls if c[0].upper() == "SEARCH"]
        self.assertEqual(len(search_calls), 1)
        _, args = search_calls[0]
        self.assertEqual(args[0], "X-GM-RAW")
        captured_arg = args[1]

        self.assertEqual(
            _unescape_imap_quoted_string(captured_arg),
            query,
            "a compound query (qualifiers + OR + parentheses + quoted phrase) "
            "driven through the public search() must come back, once the "
            "escaped quoted-string is unescaped, as the compiled Gmail-native "
            "query -- here identical to the input, since every element of it "
            "is in-grammar and already single-spaced",
        )
        # exactly one connection was created through the injected factory --
        # confirms this exercised the real ImapAdapter._conn() production path.
        self.assertEqual(len(self.factory_calls), 1)


if __name__ == "__main__":
    unittest.main()
