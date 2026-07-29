"""CR-OA-031 §S1 — the portable query grammar: one parser, one definition (RED).

Today the CLI advertises a portable query grammar (`subject:`, `from:`, `to:`,
`category:`, `newer_than:`, `has:attachment`, `OR`, quoted `"exact phrase"` —
see `vidushi_oa/_cli.py` mail-search help, ~line 1590) but **no parser exists**
(`vidushi_oa/mail/` has no `query` module). The raw query string is handed to
each provider verbatim, so `subject:Amazon` matches nothing on JMAP and
`newer_than:` is a silent no-op. §S1 fixes this by defining a single parser
that turns the portable query string into a provider-neutral query model —
the sole definition of the grammar; §S2-§S4 (separate CRs) will compile this
model down to each provider's native syntax.

These tests define the not-yet-existing public API:

    from vidushi_oa.mail.query import parse, QueryModel, QueryParseError

    parse(query: str, *, today: date | None = None) -> QueryModel

`today` is the injectable reference date `newer_than:` resolves against, so
these tests are deterministic and never depend on the wall clock.

`QueryModel` fields exercised here: `terms` (bare keywords + quoted phrases,
each phrase preserved as ONE value), `subject`, `from_`, `to`, `category`,
`has_attachment`, `newer_than` (an absolute `datetime.date`, weeks folded to
days so no provider ever receives a `w` unit), and `operator` (`"AND"` the
implicit default, or `"OR"` when the query uses the `OR` alternation).

Each test imports `vidushi_oa.mail.query` locally (inside `setUp`/the test
method) rather than at module scope, so a missing module fails each test
individually (assertion/collection-per-test RED) instead of failing the whole
file to collect.
"""
import unittest
from datetime import date, timedelta


class QueryParserCompoundQueryTest(unittest.TestCase):
    """§S1 AC1: a compound query with a bare term, a quoted-phrase qualifier,
    a relative `newer_than:`, and `has:attachment` all resolve correctly in
    one pass."""

    def setUp(self):
        from vidushi_oa.mail.query import parse

        self.parse = parse
        self.reference_date = date(2024, 3, 15)

    def test_compound_query_yields_term_subject_phrase_attachment_and_absolute_cutoff(self):
        query = 'Amazon subject:"order shipped" newer_than:2w has:attachment'

        model = self.parse(query, today=self.reference_date)

        self.assertEqual(
            model.terms, ["Amazon"],
            "the bare keyword must be the sole term -- the quoted subject "
            "phrase and the qualifiers must NOT leak into `terms`",
        )
        self.assertEqual(
            model.subject, "order shipped",
            "the quoted phrase must be preserved as ONE value, not split on "
            "its embedded spaces",
        )
        self.assertIs(model.has_attachment, True)
        self.assertEqual(
            model.newer_than, self.reference_date - timedelta(days=14),
            "newer_than:2w must resolve to an ABSOLUTE date exactly 14 days "
            "before the reference date, not be left as a relative string",
        )
        self.assertEqual(
            model.operator, "AND",
            "a query with no `OR` keyword must default to implicit-AND",
        )


class QueryParserRelativeDateUnitFoldingTest(unittest.TestCase):
    """§S1 AC2: `newer_than:1w` and `newer_than:7d` must resolve to the exact
    same absolute cutoff -- weeks fold to days in the model so no provider
    ever receives a `w` unit it cannot express."""

    def setUp(self):
        from vidushi_oa.mail.query import parse

        self.parse = parse
        self.reference_date = date(2024, 3, 15)

    def test_one_week_and_seven_days_resolve_to_the_same_absolute_cutoff(self):
        model_weeks = self.parse("newer_than:1w", today=self.reference_date)
        model_days = self.parse("newer_than:7d", today=self.reference_date)

        expected_cutoff = self.reference_date - timedelta(days=7)
        self.assertEqual(model_weeks.newer_than, expected_cutoff)
        self.assertEqual(model_days.newer_than, expected_cutoff)
        self.assertEqual(
            model_weeks.newer_than, model_days.newer_than,
            "1w and 7d must resolve to the identical absolute date",
        )


class QueryParserAlternationAndImplicitAndTest(unittest.TestCase):
    """§S1 AC3: `OR` alternation vs the implicit-AND default between bare
    terms."""

    def setUp(self):
        from vidushi_oa.mail.query import parse

        self.parse = parse

    def test_or_keyword_yields_or_alternation_between_terms(self):
        model = self.parse("a OR b")

        self.assertEqual(model.terms, ["a", "b"])
        self.assertEqual(
            model.operator, "OR",
            "the `OR` keyword must switch the model's operator to OR, not "
            "be swallowed as if it were a bare term",
        )

    def test_bare_terms_with_no_operator_yield_implicit_and(self):
        model = self.parse("a b")

        self.assertEqual(model.terms, ["a", "b"])
        self.assertEqual(
            model.operator, "AND",
            "two bare terms with no `OR` between them must default to "
            "implicit-AND",
        )


class QueryParserErrorPathTest(unittest.TestCase):
    """§S1 AC4: an unknown qualifier or unknown relative-date unit raises a
    parse error naming the offending token -- never a silent no-op."""

    def setUp(self):
        from vidushi_oa.mail.query import parse, QueryParseError

        self.parse = parse
        self.QueryParseError = QueryParseError

    def test_unknown_qualifier_raises_parse_error_naming_the_token(self):
        with self.assertRaises(self.QueryParseError) as ctx:
            self.parse("bogus:x")

        self.assertIn(
            "bogus:x", str(ctx.exception),
            "the parse error message must name the exact offending token "
            "'bogus:x', not a generic 'invalid query' message",
        )

    def test_unknown_relative_date_unit_raises_parse_error_naming_the_token(self):
        with self.assertRaises(self.QueryParseError) as ctx:
            self.parse("newer_than:3q")

        self.assertIn(
            "newer_than:3q", str(ctx.exception),
            "the parse error message must name the exact offending token "
            "'newer_than:3q' (a 'q' unit is not one of d/w/m/y)",
        )


class QueryParserBareTermsAndQuotedPhraseCoexistTest(unittest.TestCase):
    """§S1 (follows from the model being provider-neutral): bare keywords and
    a standalone quoted phrase (no qualifier prefix) coexist as ordered
    `terms`, each phrase preserved as one value."""

    def setUp(self):
        from vidushi_oa.mail.query import parse

        self.parse = parse

    def test_bare_terms_and_standalone_quoted_phrase_are_ordered_terms(self):
        model = self.parse('Amazon "order shipped" invoice')

        self.assertEqual(
            model.terms, ["Amazon", "order shipped", "invoice"],
            "a standalone quoted phrase (no qualifier prefix) must appear as "
            "ONE term among the bare keywords, in query order, not split on "
            "its embedded space nor dropped",
        )


class QueryParserFromToCategoryQualifiersTest(unittest.TestCase):
    """§S1 (follows from the model being provider-neutral): `from:`, `to:`,
    and `category:` each populate their own model field."""

    def setUp(self):
        from vidushi_oa.mail.query import parse

        self.parse = parse

    def test_from_to_and_category_qualifiers_populate_their_own_fields(self):
        model = self.parse(
            "from:alice@example.com to:bob@example.com category:purchases"
        )

        self.assertEqual(model.from_, "alice@example.com")
        self.assertEqual(model.to, "bob@example.com")
        self.assertEqual(model.category, "purchases")
        self.assertEqual(
            model.terms, [],
            "none of from:/to:/category: qualifier values may leak into the "
            "bare `terms` list",
        )


class QueryParserEmptyAndWhitespaceQueryTest(unittest.TestCase):
    """§S1: an empty or whitespace-only query is handled explicitly rather
    than raising or crashing.

    Design choice asserted here (documented, not guessed): an empty/blank
    query parses to an EMPTY `QueryModel` -- no terms, no qualifiers set,
    `has_attachment` False, `newer_than` None, implicit-AND `operator` -- so a
    provider compiler (§S2-§S4, later CRs) can treat it as "no filter" /
    match-everything rather than special-casing a `None` return or an
    exception. This is a positive, intentional contract for empty input, not
    an accidental fallthrough.
    """

    def setUp(self):
        from vidushi_oa.mail.query import parse

        self.parse = parse

    def _assert_is_empty_model(self, model):
        self.assertEqual(model.terms, [])
        self.assertIsNone(model.subject)
        self.assertIsNone(model.from_)
        self.assertIsNone(model.to)
        self.assertIsNone(model.category)
        self.assertIs(model.has_attachment, False)
        self.assertIsNone(model.newer_than)
        self.assertEqual(model.operator, "AND")

    def test_empty_string_query_parses_to_empty_model(self):
        self._assert_is_empty_model(self.parse(""))

    def test_whitespace_only_query_parses_to_empty_model(self):
        self._assert_is_empty_model(self.parse("   \t  "))


if __name__ == "__main__":
    unittest.main()
