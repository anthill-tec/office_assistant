"""CR-OA-025 §S2 — `mail-search` guidance advertises the supported grammar (RED).

Covers the "correct hints" half of CR-OA-025: now that §S1 escapes the Gmail
`X-GM-RAW` argument so quoted phrases work end-to-end, the `mail-search` verb's
help text and the skill's `references/search-recipes.md` must actually SAY
quoted phrases are a supported construct — not merely happen to work.

  - `voa mail-search --help` today (`vidushi_oa/_cli.py:1039`) carries NO
    description/help text at all: `add_parser("mail-search")` passes no
    `description=`, and `msr.add_argument("query")` has no `help=`. So the two
    help-guidance tests below fail today for the real reason (missing guidance),
    not a collection/import error.
  - `skills/vidushi-oa/references/search-recipes.md`'s "Portable qualifiers the
    verb accepts" paragraph lists `subject:`/`from:`/`OR`/parenthesised groups
    but never names quoted phrases explicitly — confirmed no "quote"/"phrase"
    token appears anywhere in the file today except the unrelated prose
    "single-phrase ones". So the recipes-guidance test below fails today too.

The final test is a characterization AXI-guard (likely already GREEN, since the
envelope logic predates this CR): a quoted-phrase `mail-search` against a fake
[GM] adapter still yields the standard `{count, results, next}` TOON envelope.
"""
import os
import re
import subprocess
import sys
import unittest
from argparse import Namespace

import vidushi_oa._cli as cli
from vidushi_oa import toon as oa_toon
from vidushi_oa.mail.base import MailAdapter, Message
from vidushi_oa.mail.client import MailClient

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STORE = os.path.join(ROOT, "scripts", "store.py")
SEARCH_RECIPES = os.path.join(ROOT, "skills", "vidushi-oa", "references", "search-recipes.md")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class MailSearchHelpGrammarTest(unittest.TestCase):
    """AC: `voa mail-search --help` states the supported compound grammar and
    includes a quoted-phrase example."""

    def _help_text(self):
        result = subprocess.run(
            [sys.executable, STORE, "mail-search", "--help"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_help_states_supported_grammar_including_or_and_quoted_phrases(self):
        help_text = self._help_text()
        self.assertRegex(
            help_text, r"\bOR\b",
            "mail-search --help must document the OR compound operator, got:\n" + help_text,
        )
        self.assertRegex(
            help_text, r"quoted[- ]phrase|exact[- ]phrase",
            "mail-search --help must explicitly name quoted-phrase support, got:\n" + help_text,
        )

    def test_help_includes_a_quoted_phrase_example(self):
        help_text = self._help_text()
        self.assertRegex(
            help_text, r'"[^"\n]+\s+[^"\n]+"',
            'mail-search --help must include a quoted-phrase example (e.g. '
            '"out for delivery"), got:\n' + help_text,
        )


class SearchRecipesExplicitQuotedPhraseSupportTest(unittest.TestCase):
    """AC: search-recipes.md explicitly names quoted phrases as a supported
    construct (not merely incidentally inside a later example) and carries no
    residual 'avoid quotes' workaround wording."""

    def _qualifiers_paragraph(self):
        body = _read(SEARCH_RECIPES)
        m = re.search(r"\*\*Portable qualifiers.*?(?=\n##|\Z)", body, re.S)
        self.assertIsNotNone(m, "expected a 'Portable qualifiers' paragraph in search-recipes.md")
        return m.group(0)

    def test_qualifiers_paragraph_explicitly_names_quoted_phrases(self):
        para = self._qualifiers_paragraph()
        self.assertRegex(
            para, r'quoted phrase|"exact phrase"|exact[- ]phrase',
            "the qualifiers paragraph must explicitly name quoted-phrase support "
            "as a supported construct, got:\n" + para,
        )

    def test_no_residual_avoid_quotes_workaround_wording(self):
        body = _read(SEARCH_RECIPES).lower()
        self.assertNotRegex(
            body, r"avoid quot|quotes break|don'?t use quot|do not use quot",
            "search-recipes.md must carry no 'avoid quotes' workaround wording",
        )


class _FakeGmailAdapter(MailAdapter):
    """A single canned [GM] message — enough to prove a quoted-phrase query still
    produces the standard AXI envelope end-to-end through `cmd_mail_search`."""

    def __init__(self, message):
        self.account = message.account
        self.source_tag = message.source_tag
        self._message = message

    def capabilities(self):
        return {"raw_query"}

    def search(self, query, folder=None, limit=None):
        return [self._message]

    def fetch_message(self, uid, folder=None):
        if uid == self._message.uid:
            return self._message
        raise KeyError(uid)

    def list_folders(self):
        return ["INBOX"]


def test_quoted_phrase_query_yields_the_standard_axi_envelope_with_gm_tagged_row(monkeypatch, capsys):
    message = Message(
        id="<gm-oa025@gmail.com>", account="gmail_main", source_tag="[GM]",
        subject="Your package is out for delivery", sender="ship@example.com",
        to="me@example.com", date="2026-07-20T10:00:00Z", uid="gm-oa025", folder="INBOX",
    )
    client = MailClient({"gmail_main": _FakeGmailAdapter(message)})
    monkeypatch.setattr(cli, "build_client", lambda **kw: client)
    original_fmt = getattr(cli, "_FMT", "toon")
    cli._FMT = "toon"
    try:
        cli.cmd_mail_search(Namespace(query='category:purchases "out for delivery"', accounts=None))
    finally:
        cli._FMT = original_fmt

    payload = oa_toon.from_toon(capsys.readouterr().out)
    assert payload["count"] == 1, f"expected exactly one [GM] row, got {payload}"
    assert payload["results"][0]["source_tag"] == "[GM]"
    assert payload["results"][0]["id"] == message.id
    assert isinstance(payload["next"], list)


if __name__ == "__main__":
    import pytest as _pytest
    raise SystemExit(_pytest.main([__file__]))
