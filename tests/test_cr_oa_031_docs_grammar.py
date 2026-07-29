"""CR-OA-031 §S6 — documentation reconciliation (RED).

`mail-search --help` and `skills/vidushi-oa/references/search-recipes.md` must state
EXACTLY the grammar `vidushi_oa/mail/query.py` accepts: every qualifier
(`subject:`/`from:`/`to:`/`category:`/`newer_than:`/`has:attachment`), the `OR`
operator, parenthesised groups, quoted phrases, the accepted relative-date unit
set `d`/`w`/`m`/`y`, and a note that `category:` is Gmail-only (JMAP and plain
IMAP both refuse it per §S2/§S4/§S5).

Baseline TODAY (pre-GREEN, confirmed by reading `vidushi_oa/_cli.py` around the
`mail-search` subparser and `skills/vidushi-oa/references/search-recipes.md`):
  - The `mail-search` help (description + `query` argument help) lists
    `subject:`, `from:`, `category:`, `newer_than:`, `has:attachment` but NEVER
    `to:` — so `test_help_lists_the_full_accepted_grammar` fails today on the
    missing `to:` qualifier.
  - The help text never mentions the accepted relative-date units at all (no
    standalone `d`/`w`/`m`/`y` token anywhere) — so
    `test_help_lists_accepted_relative_date_units` fails today.
  - The help text never says `category:` is Gmail-only —  so
    `test_help_marks_category_as_gmail_only` fails today.
  - `search-recipes.md`'s "Portable qualifiers" paragraph documents `newer_than:`
    only via the examples `3m`/`6m`/`1y` — it never names the accepted unit set
    `d`/`w`/`m`/`y` — so `test_qualifiers_paragraph_documents_full_unit_set`
    fails today.
  - Every recipe `search-recipes.md` currently documents already parses cleanly
    against today's `vidushi_oa.mail.query.parse` (all use `category:`,
    `from:`, `has:attachment`, and `newer_than:` with `m`/`y` units, all already
    accepted) — `test_every_documented_recipe_parses_without_error` is
    therefore a REGRESSION GUARD, not a failing test: it passes today and must
    keep passing after the docs are rewritten for GREEN.
"""
import os
import re
import subprocess
import sys
import unittest

from vidushi_oa.mail.query import QueryParseError, UnsupportedQualifierError, parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STORE = os.path.join(ROOT, "scripts", "store.py")
SEARCH_RECIPES = os.path.join(ROOT, "skills", "vidushi-oa", "references", "search-recipes.md")

#: The parser's full accepted qualifier set (CR-OA-031 §S1), labelled for
#: subTest reporting.
ACCEPTED_QUALIFIERS = {
    "subject:": "subject:",
    "from:": "from:",
    "to:": "to:",
    "category:": "category:",
    "newer_than:": "newer_than:",
    "has:attachment": "has:attachment",
}

#: The parser's full accepted `newer_than:` unit set (CR-OA-031 §S1: weeks fold
#: to days, months/years fold to their day counts — all four units are valid
#: input).
ACCEPTED_UNITS = ("d", "w", "m", "y")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _extract_documented_recipes(body):
    """Pull every `voa mail-search '<query>'` invocation out of the doc.

    Recipes live inside fenced code blocks as whole lines of the exact form
    `voa mail-search '<query>'`; the intro paragraph's own literal template
    `voa mail-search '<query>'` is excluded (its query is the placeholder
    string `<query>`, not a real recipe).
    """
    recipes = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped.startswith("voa mail-search '"):
            continue
        start = stripped.index("'") + 1
        end = stripped.rindex("'")
        query = stripped[start:end]
        if query == "<query>":
            continue
        recipes.append(query)
    return recipes


class MailSearchHelpGrammarDocumentationTest(unittest.TestCase):
    """AC §S6 bullet 1: `mail-search --help` states exactly the parser's
    accepted grammar — every qualifier, `OR`, parenthesised groups, quoted
    phrases, the `d`/`w`/`m`/`y` units, and the Gmail-only note on
    `category:`."""

    def _help_text(self):
        result = subprocess.run(
            [sys.executable, STORE, "mail-search", "--help"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_help_lists_the_full_accepted_grammar(self):
        # Plain assertion collection (NOT subTest): under plain `pytest` (no
        # `pytest-subtests` plugin, which this project does not have
        # installed), a `unittest` subTest failure is reported but does NOT
        # flip the enclosing test to FAILED — it would silently read as a
        # first-run PASS. Collecting misses into a list and asserting on the
        # list keeps the failure real under both `unittest` and `pytest`.
        help_text = self._help_text()

        missing_qualifiers = [
            token for token in ACCEPTED_QUALIFIERS.values() if token not in help_text
        ]
        self.assertEqual(
            missing_qualifiers, [],
            "mail-search --help must list every accepted qualifier "
            f"(today's help omits {missing_qualifiers}), got:\n{help_text}",
        )

        self.assertRegex(
            help_text, r"\bOR\b",
            f"mail-search --help must document the OR operator, got:\n{help_text}",
        )
        self.assertRegex(
            help_text, r"parenthesi[sz]ed group",
            f"mail-search --help must document parenthesised groups, got:\n{help_text}",
        )
        self.assertRegex(
            help_text, r'quoted[- ]phrase|exact[- ]phrase',
            f"mail-search --help must document quoted-phrase matching, got:\n{help_text}",
        )

    def test_help_lists_accepted_relative_date_units(self):
        help_text = self._help_text()
        missing = [
            unit for unit in ACCEPTED_UNITS
            if not re.search(rf"\b{unit}\b", help_text)
        ]
        self.assertEqual(
            missing, [],
            "mail-search --help must document the full accepted newer_than: "
            f"unit set d/w/m/y; missing unit(s) {missing} today, got:\n{help_text}",
        )

    def test_help_marks_category_as_gmail_only(self):
        help_text = self._help_text()
        self.assertRegex(
            help_text,
            r"category:[^\n]{0,120}Gmail.only|Gmail.only[^\n]{0,120}category:",
            "mail-search --help must mark `category:` as Gmail-only (JMAP and "
            f"plain IMAP both refuse it per §S2/§S4/§S5), got:\n{help_text}",
        )


class DocumentedRecipesParseTest(unittest.TestCase):
    """AC §S6 bullet 2: every documented recipe in search-recipes.md parses
    without error — the guard against a shipped recipe using a
    qualifier/unit the parser rejects."""

    def test_every_documented_recipe_parses_without_error(self):
        body = _read(SEARCH_RECIPES)
        recipes = _extract_documented_recipes(body)
        self.assertGreater(
            len(recipes), 0,
            "expected to find at least one `voa mail-search '<query>'` recipe "
            "in search-recipes.md",
        )

        failures = []
        for query in recipes:
            try:
                parse(query)
            except (QueryParseError, UnsupportedQualifierError) as exc:
                failures.append((query, str(exc)))

        self.assertEqual(
            failures, [],
            "the following documented recipes fail to parse:\n"
            + "\n".join(f"  {query!r} -> {reason}" for query, reason in failures),
        )


class SearchRecipesUnitDocumentationTest(unittest.TestCase):
    """AC §S6 bullet 1 (docs half): search-recipes.md's qualifiers paragraph
    documents the SAME accepted unit set (`d`/`w`/`m`/`y`) as the parser and
    the CLI help — today it shows only the `3m`/`6m`/`1y` examples."""

    def _qualifiers_paragraph(self):
        body = _read(SEARCH_RECIPES)
        m = re.search(r"\*\*Portable qualifiers.*?(?=\n##|\Z)", body, re.S)
        self.assertIsNotNone(m, "expected a 'Portable qualifiers' paragraph in search-recipes.md")
        return m.group(0)

    def test_qualifiers_paragraph_documents_full_unit_set(self):
        para = self._qualifiers_paragraph()
        missing = [
            unit for unit in ACCEPTED_UNITS
            if not re.search(rf"\b{unit}\b", para)
        ]
        self.assertEqual(
            missing, [],
            "search-recipes.md's qualifiers paragraph must document the full "
            f"accepted newer_than: unit set d/w/m/y; missing unit(s) {missing} "
            f"today, got:\n{para}",
        )


if __name__ == "__main__":
    unittest.main()
