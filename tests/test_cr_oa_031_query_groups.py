"""CR-OA-031 §S1/§S2/§S3 -- nestable parenthesised GROUPS in the portable query
grammar, and their recursive compilation to JMAP and Gmail (RED).

`docs/changes/CR-OA-031-portable-query-translation.md` was amended
2026-07-30: `voa mail-search --help` advertises "parenthesised groups" in both
its description and its `query` help, and such queries already work on the
Gmail passthrough path today. The original §S1 grammar/model was flat
(`QueryModel` in `vidushi_oa/mail/query.py` has no notion of a group at all --
`parse()` only ever sets `terms`/`subject`/`from_`/`to`/`category`/
`has_attachment`/`newer_than`/`operator`), so as it stands this CR would turn
a documented, working query into a `QueryParseError` -- a capability
regression against our own advertised grammar. This file is the RED coverage
for the corrected spec: the model becomes a small TREE, and each compiler
(§S2 JMAP, §S3 Gmail) recurses over it.

THE TREE API THIS FILE DEFINES (not yet implemented -- GREEN's job):

    from vidushi_oa.mail.query import parse, QueryModel, QueryNode, QueryParseError

`QueryModel` gains a new field, `root: QueryNode`, holding the whole parsed
query as a tree (populated for every query, grouped or not). `QueryNode` is a
plain, provider-neutral dataclass with exactly two shapes:

  - a GROUP node: `operator` is `"AND"` or `"OR"`; `children` is a non-empty
    list of child `QueryNode`s (each a leaf OR a nested group -- groups nest
    arbitrarily); `term`/`qualifier`/`value` are all `None`.
  - a LEAF node: `children` is `[]`; EITHER `term` holds a bare keyword /
    quoted phrase (`qualifier`/`value` are `None`), OR `qualifier` holds one
    of `"subject"`/`"from_"`/`"to"`/`"category"`/`"newer_than"`/
    `"has_attachment"` and `value` holds its parsed value (a plain `str` for
    the free-form qualifiers, the resolved absolute `datetime.date` for
    `newer_than`, `True` for `has_attachment`).

A bare, non-parenthesised query (e.g. `"a OR b"`, `"a b"`) still parses to a
`root` that is a single GROUP node equivalent to today's flat model output --
this file does not touch that path (see the docstring note on the existing
flat-field tests below). `QueryNode` is expected to be a `@dataclass` (default
`eq=True`) so two trees can be compared with plain `==`, which is exactly how
these tests assert structure: they build the EXPECTED tree by hand out of the
same `QueryNode` class and compare it to `model.root`.

NOTE on the existing flat-field tests (`tests/test_cr_oa_031_query_parser.py`)
-- NOT edited here, per instructions. Those tests read `model.terms`,
`model.subject`, `model.from_`, etc. directly and never touch `model.root`.
Whether GREEN keeps populating those flat fields (as a top-level convenience
projection of a non-grouped `root`) or retires them in favour of `root` alone
is a GREEN/orchestrator reconciliation call -- this file intentionally does
not assume either way for the FLAT (non-grouped) queries that file covers.
For AC4 below (a qualifier INSIDE a group), this file additionally asserts
the flat top-level fields stay unset -- that assertion is required by the AC
text itself ("keeps each qualifier INSIDE the group, not flattened away"),
and is scoped to the grouped-query case only, never to the existing flat
tests' queries.

Each test imports `vidushi_oa.mail.query` locally so a missing/incomplete
symbol (`QueryNode` does not exist yet; `QueryModel` has no `root` field yet)
fails each test individually rather than failing the whole file to collect.
"""
import unittest
from datetime import date


class QueryParserGroupTopLevelAndOfGroupAndTermTest(unittest.TestCase):
    """§S1 new AC: `parse("(a OR b) c")` yields a tree whose top level is an
    AND of [a group holding an OR of `a`,`b`] and the term `c`. Fails today:
    `QueryNode` does not exist and `QueryModel` has no `root` field, so this
    raises `ImportError`/`AttributeError` immediately; even once `QueryNode`
    exists, `parse()` does not recognise parentheses at all yet."""

    def setUp(self):
        from vidushi_oa.mail.query import parse, QueryNode

        self.parse = parse
        self.QueryNode = QueryNode

    def test_group_or_followed_by_bare_term_yields_and_of_group_and_term(self):
        model = self.parse("(a OR b) c")

        expected = self.QueryNode(
            operator="AND",
            children=[
                self.QueryNode(
                    operator="OR",
                    children=[
                        self.QueryNode(term="a"),
                        self.QueryNode(term="b"),
                    ],
                ),
                self.QueryNode(term="c"),
            ],
        )
        self.assertEqual(
            model.root, expected,
            "the top-level node must be an AND group whose first child is "
            "the OR(a, b) group and whose second child is the bare term "
            "'c' -- not a flat list, and not a parse error",
        )


class QueryParserNestedGroupsTest(unittest.TestCase):
    """§S1 new AC: groups NEST -- `parse("((a OR b) c) OR d")` parses to the
    corresponding nested tree (an outer OR whose first child is the exact AND
    group from the top-level test above, and whose second child is the bare
    term `d`)."""

    def setUp(self):
        from vidushi_oa.mail.query import parse, QueryNode

        self.parse = parse
        self.QueryNode = QueryNode

    def test_doubly_nested_group_parses_to_corresponding_nested_tree(self):
        model = self.parse("((a OR b) c) OR d")

        inner_or = self.QueryNode(
            operator="OR",
            children=[self.QueryNode(term="a"), self.QueryNode(term="b")],
        )
        inner_and = self.QueryNode(
            operator="AND", children=[inner_or, self.QueryNode(term="c")]
        )
        expected = self.QueryNode(
            operator="OR", children=[inner_and, self.QueryNode(term="d")]
        )

        self.assertEqual(
            model.root, expected,
            "a doubly-nested group must produce the exact corresponding "
            "nested QueryNode tree -- an outer OR over the inner "
            "'(a OR b) c' AND-group and the bare term 'd'",
        )


class QueryParserUnbalancedParenthesesTest(unittest.TestCase):
    """§S1 new AC: unbalanced parentheses raise `QueryParseError` naming the
    offending token -- both an unclosed opening paren and a stray closing
    paren with no matching open."""

    def setUp(self):
        from vidushi_oa.mail.query import parse, QueryParseError

        self.parse = parse
        self.QueryParseError = QueryParseError

    def test_unclosed_opening_paren_raises_parse_error_naming_the_paren(self):
        with self.assertRaises(self.QueryParseError) as ctx:
            self.parse("(a OR b")

        self.assertIn(
            "(", str(ctx.exception),
            "the parse error for an unclosed group must name the offending "
            "'(' token, not a generic 'invalid query' message",
        )

    def test_stray_closing_paren_raises_parse_error_naming_the_paren(self):
        with self.assertRaises(self.QueryParseError) as ctx:
            self.parse("a) b")

        self.assertIn(
            ")", str(ctx.exception),
            "the parse error for a stray, unmatched ')' must name the "
            "offending ')' token, not a generic 'invalid query' message",
        )


class QueryParserGroupCarryingQualifiersTest(unittest.TestCase):
    """§S1 new AC: a group carrying qualifiers keeps each qualifier INSIDE
    the group, not flattened away -- `parse("(category:purchases OR
    subject:refund)")` must produce a group node whose two children are the
    `category` and `subject` qualifier leaves, and the top-level FLAT fields
    (`model.category`, `model.subject`) must stay unset since both
    qualifiers live inside the group, never at the top level."""

    def setUp(self):
        from vidushi_oa.mail.query import parse, QueryNode

        self.parse = parse
        self.QueryNode = QueryNode

    def test_group_with_two_qualifiers_keeps_them_inside_the_group(self):
        model = self.parse("(category:purchases OR subject:refund)")

        expected = self.QueryNode(
            operator="OR",
            children=[
                self.QueryNode(qualifier="category", value="purchases"),
                self.QueryNode(qualifier="subject", value="refund"),
            ],
        )
        self.assertEqual(
            model.root, expected,
            "the group's two qualifiers must be represented as qualifier "
            "leaves inside the OR group, in query order",
        )
        self.assertIsNone(
            model.category,
            "the group's category: qualifier must NOT be flattened up onto "
            "the top-level model.category field -- it lives inside the "
            "group only",
        )
        self.assertIsNone(
            model.subject,
            "the group's subject: qualifier must NOT be flattened up onto "
            "the top-level model.subject field -- it lives inside the "
            "group only",
        )


# ---------------------------------------------------------------------------
# §S2 -- JMAP recursive compilation of a nested group.
# ---------------------------------------------------------------------------

SESSION_URL = "https://api.fastmail.com/jmap/session"
API_URL = "https://api.fastmail.com/jmap/api/"
ACCOUNT_ID = "u1234567"

_CANNED_SESSION = {
    "apiUrl": API_URL,
    "accounts": {
        ACCOUNT_ID: {
            "name": "you@fastmail.com",
            "isPersonal": True,
            "accountCapabilities": {"urn:ietf:params:jmap:mail": {}},
        },
    },
    "primaryAccounts": {"urn:ietf:params:jmap:mail": ACCOUNT_ID},
    "capabilities": {"urn:ietf:params:jmap:mail": {}},
}

#: A minimal batched response that lets `_parse()` succeed cleanly (an empty,
#: legitimate result) -- this test only cares about the REQUEST body
#: `search()` sends, never the parsed return value. Mirrors the fake-transport
#: seam `tests/test_jmap_query_filter_compilation.py` already uses.
_CANNED_EMPTY_RESULT = {
    "methodResponses": [
        ["Email/query", {"accountId": ACCOUNT_ID, "ids": []}, "0"],
        ["Email/get", {"accountId": ACCOUNT_ID, "list": [], "notFound": []}, "1"],
    ],
}


class _FakeTransport:
    """Records every `(method, url, headers, body)` call and returns canned
    `(status, dict)` tuples shaped like real JMAP HTTP responses -- no
    network. Same shape as the existing `test_jmap_query_filter_compilation.py`
    fake, duplicated here so this file stays self-contained."""

    def __init__(self, session_response=None, post_response=None):
        self.session_response = session_response or _CANNED_SESSION
        self.post_response = post_response or _CANNED_EMPTY_RESULT
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        if method == "GET":
            return 200, self.session_response
        return 200, self.post_response

    def calls_of(self, method):
        return [c for c in self.calls if c[0] == method]


def _sent_filter(query):
    """Run `JmapAdapter.search(query)` against a fresh `_FakeTransport` and
    return the `filter` object placed in the batched `Email/query` request."""
    from vidushi_oa.mail.jmap import JmapAdapter

    transport = _FakeTransport()
    adapter = JmapAdapter(
        account="fastmail_main", source_tag="[FM]", token="secret-token",
        session_url=SESSION_URL, transport=transport,
    )
    adapter.search(query)
    _, _, _, body = transport.calls_of("POST")[0]
    query_call = body["methodCalls"][0]
    assert query_call[0] == "Email/query"
    return query_call[1]["filter"]


class JmapNestedGroupCompilationTest(unittest.TestCase):
    """§S2 new AC: a nested group compiles to NESTED JMAP `FilterOperator`s --
    `(a OR b) c` -> `{"operator": "AND", "conditions": [{"operator": "OR",
    "conditions": [{"text": "a"}, {"text": "b"}]}, {"text": "c"}]}`. Fails
    today two ways: (1) `parse()` raises `QueryParseError` on the `(` before
    the adapter ever gets a model to compile, since groups aren't recognised
    yet; (2) even once parsing succeeds, `compile_filter` has no recursion
    over child groups, so this exact NESTED shape is not produced."""

    def test_group_or_then_and_with_bare_term_compiles_to_nested_filter_operators(self):
        filter_sent = _sent_filter("(a OR b) c")

        self.assertEqual(
            filter_sent,
            {
                "operator": "AND",
                "conditions": [
                    {
                        "operator": "OR",
                        "conditions": [{"text": "a"}, {"text": "b"}],
                    },
                    {"text": "c"},
                ],
            },
            f"expected a NESTED AND-over-OR JMAP filter, got {filter_sent!r} "
            "-- the compiler must recurse into the group instead of "
            "flattening or dropping it",
        )


# ---------------------------------------------------------------------------
# §S3 -- Gmail (X-GM-RAW) recursive compilation of a nested group, native
# parentheses.
# ---------------------------------------------------------------------------


class _FakeIMAP:
    """Records every `.uid(cmd, *args)` call and returns a canned SEARCH
    response; FETCH is irrelevant to this compilation test but must return an
    empty-but-well-formed payload so `search()` completes without error. Same
    shape as `tests/test_cr_oa_031_gmail_query_compilation.py`'s fake,
    duplicated here so this file stays self-contained."""

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
    quoted-string (`"`-delimited, `\\`-escaped) `X-GM-RAW` argument -- the
    same helper `tests/test_cr_oa_031_gmail_query_compilation.py` uses, so
    CR-025's escaping contract is proven to still hold. Raises
    AssertionError if `arg` is not a well-formed quoted-string."""
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
    """Run `GmailImapAdapter.search(query)` against a fresh `_FakeIMAP` and
    return the recovered (unescaped) Gmail-native query string embedded in
    the `X-GM-RAW` argument."""
    from vidushi_oa.mail.imap import GmailImapAdapter

    fake = _FakeIMAP(search_response=("OK", [b""]))
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


class GmailNestedGroupNativeParenthesesCompilationTest(unittest.TestCase):
    """§S3 new AC: a nested group emits Gmail's NATIVE parentheses --
    `(a OR b) c` -> the compiled `X-GM-RAW` string contains `(a OR b) c`,
    asserted via the existing conn_factory/fake-IMAP seam. Fails today two
    ways: (1) `parse()` raises `QueryParseError` on the `(` (groups aren't
    recognised yet), so `search()` never even gets to build a query; (2) even
    once parsing succeeds, today's raw passthrough would merely echo the
    input verbatim rather than a real recursive compile from the model --
    this test's exact-equality assertion pins the single-spaced,
    recursively-rendered form."""

    def test_group_or_then_and_with_bare_term_emits_native_parentheses(self):
        native = _searched_native_query("(a OR b) c")

        self.assertEqual(
            native, "(a OR b) c",
            "a nested OR-group followed by a bare term must compile to "
            "Gmail's own parenthesised native form '(a OR b) c', not a "
            "parse error and not a mangled/flattened rendering",
        )


if __name__ == "__main__":
    unittest.main()
