"""CR-OA-031 §S3 — Gmail (`X-GM-RAW`) compilation from the portable query model,
native operators, provider-valid units (RED).

Today `GmailImapAdapter.search()` (`vidushi_oa/mail/imap.py` ~line 483) never
calls `vidushi_oa.mail.query.parse()` at all -- it escapes/quotes the RAW query
string verbatim and sends it straight through as the `X-GM-RAW` IMAP argument:

    escaped = query.replace("\\\\", "\\\\\\\\").replace('"', '\\\\"')
    conn.uid("SEARCH", "X-GM-RAW", f'"{escaped}"')

That is exactly right for a Gmail-native query already (Gmail natively
understands `subject:`/`from:`/`to:`/`has:attachment`/`category:` and quoted
phrases -- the portable grammar deliberately mirrors Gmail's own syntax for
those). The one place raw passthrough is WRONG is `newer_than:` units: the
portable grammar accepts `d`/`w`/`m`/`y` (folding each to an absolute cutoff
`date` in `query.py`), but Gmail's own `newer_than:` operator only parses
`d`/`m`/`y` -- a bare `w` is not a unit Gmail recognises, so `newer_than:1w`
reaches Gmail **verbatim** and matches nothing. Even `m`/`y` are wrong to pass
through raw: Gmail's own `newer_than:3m` means "3 **calendar** months" (28-31
days each), not the portable grammar's calendar-free 30-day month -- so §S3
recompiles the model's already-resolved absolute cutoff back into a **day
count** (`d`) for every relative unit, never re-emitting `w`/`m`/`y` literally.

§S3 fixes this by routing `search()` through `parse(query)` -> a Gmail
compiler that reconstructs the native `X-GM-RAW` string from the `QueryModel`
(bare terms, `subject:`/`from:`/`to:` values re-quoted when they contain
whitespace, `has:attachment`, `category:`, `newer_than:<N>d`, `OR`/implicit-AND)
instead of echoing the raw string.

Because most individual qualifiers are ALREADY valid Gmail syntax, a query
that only exercises them (no `newer_than:` unit mismatch) is silently
"correct" under today's raw passthrough too, by coincidence -- that would make
a naive assertion pass against the current no-op-equivalent code, which is
exactly the kind of test the RED discipline forbids ("would this pass against
a no-op stub?"). To force these tests to genuinely fail today and only pass
once `search()` actually goes through `parse()` + a compiler, every query
below that isn't a `newer_than:` case is written with REDUNDANT internal
whitespace (extra spaces between tokens); raw passthrough preserves that
whitespace verbatim, while a real compile-from-model reconstructs the string
by re-joining individually-rendered tokens with a single space. Each test
therefore asserts BOTH the expected native-Gmail content AND the absence of
any leftover double-space -- the second assertion is what actually fails
against today's code.

Tests drive the *public* `GmailImapAdapter.search(query)` end-to-end against a
fake IMAP connection (the same `FakeIMAP` shape as
`tests/test_cr_oa_025_gmail_raw_quoting.py`), capture the exact `<arg>` handed
to `conn.uid("SEARCH", "X-GM-RAW", <arg>)`, and recover the underlying Gmail
native query string via the same IMAP-quoted-string unescaping helper CR-025
uses -- so CR-025's escaping contract (embedded `"` -> `\\"`, `\\` -> `\\\\`)
is exercised and must stay intact through the new compiler, not just before
it.
"""
import unittest

from vidushi_oa.mail.imap import GmailImapAdapter


class FakeIMAP:
    """Records every `.uid(cmd, *args)` call and returns a canned SEARCH
    response; FETCH is irrelevant to these compilation tests but must return
    an empty-but-well-formed payload so `search()` completes without error."""

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
    """Recover the literal Gmail-native query string from an RFC 3501
    quoted-string (`"`-delimited, `\\`-escaped) `X-GM-RAW` argument -- the same
    helper `tests/test_cr_oa_025_gmail_raw_quoting.py` uses -- so CR-025's
    escaping contract is proven to still hold through the new compiler.
    Raises AssertionError if `arg` is not a well-formed quoted-string."""
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


def _searched_native_query(query):
    """Run `GmailImapAdapter.search(query)` against a fresh `FakeIMAP` and
    return the recovered (unescaped) Gmail-native query string that was
    embedded in the `X-GM-RAW` argument."""
    fake = FakeIMAP(search_response=("OK", [b""]))
    factory, _ = _make_conn_factory(fake)
    adapter = GmailImapAdapter(
        account="gmail_main",
        source_tag="[GM]",
        host="imap.gmail.com",
        user="me@gmail.com",
        password="app-pw",
        conn_factory=factory,
    )
    adapter.search(query)
    search_calls = [c for c in fake.uid_calls if c[0].upper() == "SEARCH"]
    assert len(search_calls) == 1, f"expected exactly one SEARCH call, got {search_calls!r}"
    _, args = search_calls[0]
    assert args[0] == "X-GM-RAW", f"expected X-GM-RAW, got {args[0]!r}"
    return _unescape_imap_quoted_string(args[1])


class GmailNewerThanUnitTranslationTest(unittest.TestCase):
    """§S3 AC1: `newer_than:<N><unit>` compiles to Gmail's own `newer_than:<M>d`
    form -- never the literal portable unit -- because Gmail's `newer_than:`
    only parses `d`/`m`/`y` (no `w`), and even its own `m`/`y` mean calendar
    months/years, not the portable grammar's calendar-free 30/365-day folding.
    Fails today: `search()` sends the raw token (`1w`, `2w`, `3m`, `1y`)
    straight through, unrecognised units and all."""

    def test_newer_than_one_week_compiles_to_gmail_native_seven_days(self):
        native = _searched_native_query("newer_than:1w")

        self.assertEqual(
            native, "newer_than:7d",
            "newer_than:1w must compile to Gmail's own newer_than:7d form",
        )
        self.assertNotIn(
            "1w", native,
            "the literal portable unit 'w' must never reach Gmail -- it is "
            "not one of Gmail's own newer_than: units",
        )

    def test_newer_than_two_weeks_compiles_to_gmail_native_fourteen_days(self):
        native = _searched_native_query("newer_than:2w")

        self.assertEqual(
            native, "newer_than:14d",
            "newer_than:2w must compile to Gmail's own newer_than:14d form",
        )
        self.assertNotIn("2w", native)

    def test_newer_than_three_months_compiles_to_gmail_native_ninety_days(self):
        # The parser folds `m` to a calendar-free 30 days/month (query.py
        # `_UNIT_DAYS`), so 3m -> 90 days. §S3 must emit that as Gmail's `d`
        # unit, not re-emit `3m` (which Gmail would read as 3 CALENDAR
        # months -- a different, drifting cutoff).
        native = _searched_native_query("newer_than:3m")

        self.assertEqual(
            native, "newer_than:90d",
            "newer_than:3m must compile to Gmail's own newer_than:90d form "
            "(the portable grammar's calendar-free 30-day month), never "
            "Gmail's own calendar-month newer_than:3m",
        )
        self.assertNotIn("3m", native)

    def test_newer_than_one_year_compiles_to_gmail_native_three_hundred_sixty_five_days(self):
        # `y` folds to a calendar-free 365 days/year (query.py `_UNIT_DAYS`),
        # so 1y -> 365 days, again emitted as Gmail's `d` unit.
        native = _searched_native_query("newer_than:1y")

        self.assertEqual(
            native, "newer_than:365d",
            "newer_than:1y must compile to Gmail's own newer_than:365d form",
        )
        self.assertNotIn("1y", native)


class GmailCategoryAndQuotedPhraseCompilationTest(unittest.TestCase):
    """§S3 AC2: `category:purchases "out for delivery"` compiles to `X-GM-RAW`
    preserving the native `category:` operator AND the quoted phrase, with
    CR-OA-025's escaping intact. Extra internal whitespace forces this to
    genuinely exercise the model-based compiler rather than passing
    trivially against today's raw passthrough (which happens to already
    produce valid Gmail syntax for this qualifier/phrase combination)."""

    def test_category_operator_and_quoted_phrase_preserved_with_escaping_intact(self):
        # Redundant internal whitespace: raw passthrough preserves it
        # verbatim; a real compile-from-model reconstruction re-joins the
        # parsed tokens (`category:purchases`, `out for delivery`) with a
        # single space, collapsing it.
        query = 'category:purchases    "out for delivery"'

        native = _searched_native_query(query)

        self.assertEqual(
            native, 'category:purchases "out for delivery"',
            "the native category: operator and the quoted phrase must both "
            "survive compilation, single-spaced (not the redundant "
            "whitespace of the raw input) -- CR-025's escaping contract is "
            "exercised by _searched_native_query's use of "
            "_unescape_imap_quoted_string, which raises on any unbalanced "
            "quoting",
        )
        self.assertNotIn(
            "  ", native,
            "the compiled native query must not carry the raw input's "
            "redundant whitespace -- that only happens if search() actually "
            "recompiles from the parsed QueryModel instead of echoing the "
            "raw string",
        )


class GmailNativeQualifierMappingTest(unittest.TestCase):
    """§S3 AC (other qualifiers): `subject:`, `from:`, `to:`, `has:attachment`
    map to their native Gmail operators, and bare terms/quoted phrases
    survive. Redundant whitespace again forces real compilation rather than
    a passthrough that happens to already be valid Gmail syntax."""

    def test_subject_from_to_and_has_attachment_map_to_native_gmail_operators(self):
        query = (
            'subject:"order shipped"   from:vendor@example.com   '
            'to:me@example.com   has:attachment'
        )

        native = _searched_native_query(query)

        self.assertIn(
            'subject:"order shipped"', native,
            "subject: must map to Gmail's own subject: operator, quoted "
            "since the value contains whitespace",
        )
        self.assertIn("from:vendor@example.com", native,
                      "from: must map to Gmail's own from: operator")
        self.assertIn("to:me@example.com", native,
                      "to: must map to Gmail's own to: operator")
        self.assertIn("has:attachment", native,
                      "has:attachment must map to Gmail's own has:attachment operator")
        self.assertNotIn(
            "  ", native,
            "the compiled native query must not carry the raw input's "
            "redundant whitespace between qualifiers -- proves this went "
            "through the parsed QueryModel, not a raw-string passthrough",
        )

    def test_bare_terms_and_quoted_phrases_survive_compilation(self):
        query = 'Amazon   "out for delivery"'

        native = _searched_native_query(query)

        self.assertEqual(
            native, 'Amazon "out for delivery"',
            "a bare term and a quoted phrase must both survive compilation, "
            "single-spaced and the phrase still quoted",
        )
        self.assertNotIn("  ", native)


class GmailOperatorCompilationTest(unittest.TestCase):
    """§S3 AC (implicit-AND / OR): both alternation forms compile to Gmail's
    own forms -- space-separated implicit-AND, and the literal `OR` keyword
    for alternation. Redundant whitespace again forces real compilation."""

    def test_or_alternation_compiles_to_gmail_native_or_form(self):
        query = "a   OR   b"

        native = _searched_native_query(query)

        self.assertEqual(
            native, "a OR b",
            "OR alternation must compile to Gmail's own single-spaced "
            "'a OR b' form, not the raw input's redundant whitespace",
        )

    def test_implicit_and_compiles_to_gmail_native_space_separated_form(self):
        query = "c   d"

        native = _searched_native_query(query)

        self.assertEqual(
            native, "c d",
            "implicit-AND must compile to Gmail's own single-spaced, "
            "space-separated 'c d' form -- Gmail treats space-separated "
            "terms as AND by default, with no OR keyword introduced",
        )
        self.assertNotIn("OR", native,
                         "implicit-AND must never introduce a literal OR keyword")


if __name__ == "__main__":
    unittest.main()
