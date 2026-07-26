"""CR-OA-021 — the unified skill's mail access repoints to `voa mail-*` verbs.

Encodes the CR's grep acceptance gates as a durable regression guard, mirroring
CR-016's fast structural-proxy style (`tests/test_cr_oa_016_skill_bundle.py`):
a grep/structural check over `skills/vidushi-oa/SKILL.md` + `references/`, NOT the
live `agentskills validate` (that needs network/skills-ref; the release gate runs
the authoritative one).

  §S1 "Mailboxes & search" section drives search via `voa mail-search`, not the
      raw FastmailMCP/Gmail-connector search verbs (`search_email`/`search_threads`);
      Yahoo (`[YH]`) named alongside Fastmail (`[FM]`) + Gmail (`[GM]`).
  §S1 "Deep-sweep mode (read-only)" section reads via `voa mail-search`; its
      read-only guarantee (`read-only` / `mutates nothing`) is restated unchanged.
  §S3 references/search-recipes.md expresses `voa mail-search` query forms, not raw
      per-MCP search calls.
  §S4 references/mail-setup.md exists, is linked from SKILL.md, documents
      `mail-auth` + `voa doctor` onboarding with per-provider credential steps and
      the "agent never handles the secret" guarantee.
"""
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SKILL_DIR = os.path.join(ROOT, "skills", "vidushi-oa")
SKILL = os.path.join(SKILL_DIR, "SKILL.md")
REFS = os.path.join(SKILL_DIR, "references")
SEARCH_RECIPES = os.path.join(REFS, "search-recipes.md")
MAIL_SETUP = os.path.join(REFS, "mail-setup.md")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _section(body, heading_pattern):
    """Slice a '## Heading...' section up to the next '## ' heading or EOF.

    ``^## `` (two hashes + space) only matches level-2 headings — a level-3
    ``### Domain`` heading inside the section does NOT terminate it early.
    """
    return re.search(r"^## " + heading_pattern + r".*?(?=^## |\Z)", body, re.S | re.M)


class MailboxesSearchSectionTest(unittest.TestCase):
    """§S1 — 'Mailboxes & search' repointed to `voa mail-search`."""

    def _mailboxes_section(self):
        m = _section(_read(SKILL), r"Mailboxes & search")
        self.assertIsNotNone(m, "a '## Mailboxes & search' section is required")
        return m.group(0)

    def test_section_instructs_mail_search_verb(self):
        self.assertIn("mail-search", self._mailboxes_section())

    def test_section_no_longer_instructs_raw_mcp_search_verbs(self):
        sec = self._mailboxes_section()
        self.assertNotIn("search_email", sec,
                          "raw FastmailMCP search_email must be gone from this section")
        self.assertNotIn("search_threads", sec,
                          "raw Gmail-connector search_threads must be gone from this section")

    def test_yahoo_named_alongside_fastmail_and_gmail(self):
        sec = self._mailboxes_section()
        self.assertIn("[FM]", sec)
        self.assertIn("[GM]", sec)
        self.assertIn("[YH]", sec, "Yahoo's source tag must be named alongside Fastmail + Gmail")
        self.assertIn("Yahoo", sec)


class DeepSweepReadPathTest(unittest.TestCase):
    """§S1 — Deep-sweep mode's read path repointed to `voa mail-search`."""

    def _deep_sweep_section(self):
        m = _section(_read(SKILL), r"Deep-sweep mode \(read-only\)")
        self.assertIsNotNone(m, "a '## Deep-sweep mode (read-only)' section is required")
        return m.group(0)

    def test_section_reads_via_mail_search_verb(self):
        self.assertIn("mail-search", self._deep_sweep_section())

    def test_read_only_guarantee_restated(self):
        sec = self._deep_sweep_section()
        self.assertIn("read-only", sec)
        self.assertIn("mutates nothing", sec)


class SearchRecipesRewrittenTest(unittest.TestCase):
    """§S3 — references/search-recipes.md expresses `voa mail-search` forms."""

    def test_documents_mail_search_query_forms(self):
        self.assertIn("mail-search", _read(SEARCH_RECIPES))

    def test_raw_mcp_search_mechanism_tokens_absent(self):
        body = _read(SEARCH_RECIPES)
        self.assertNotIn("search_email", body)
        self.assertNotIn("search_threads", body)


class MailSetupOnboardingTest(unittest.TestCase):
    """§S4 — references/mail-setup.md onboarding guide."""

    def test_mail_setup_file_exists(self):
        self.assertTrue(os.path.isfile(MAIL_SETUP), "references/mail-setup.md must exist")

    def test_skill_links_mail_setup(self):
        self.assertIn("references/mail-setup.md", _read(SKILL),
                       "SKILL.md must link references/mail-setup.md")

    def test_documents_mail_auth_and_doctor_verbs(self):
        body = _read(MAIL_SETUP)
        self.assertIn("mail-auth", body)
        self.assertIn("voa doctor", body)

    def test_documents_per_provider_credential_generation(self):
        body = _read(MAIL_SETUP).lower()
        self.assertRegex(body, r"app[- ]password",
                          "must document generating an app password (Gmail/Yahoo IMAP)")
        self.assertRegex(body, r"jmap|token",
                          "must document the Fastmail read-only JMAP token")
        self.assertIn("xoauth2", body, "must document the Gmail-Workspace XOAUTH2 path")

    def test_states_agent_never_handles_the_secret(self):
        body = _read(MAIL_SETUP).lower()
        self.assertRegex(
            body,
            r"never\b.{0,80}secret|secret\b.{0,80}never",
            "must state the agent never sees/handles the secret",
        )


class StructuralProxyTest(unittest.TestCase):
    """Fast structural proxy (mirrors CR-016): frontmatter + reference wiring, hermetic
    — no live `agentskills validate` (network/skills-ref; the release gate runs it)."""

    def test_frontmatter_name_matches_dir_and_description_present(self):
        txt = _read(SKILL)
        m = re.match(r"^---\n(.*?)\n---\n", txt, re.S)
        self.assertIsNotNone(m, "SKILL.md must open with YAML frontmatter")
        fm = m.group(1)
        name = re.search(r"^name:\s*(.+)$", fm, re.M).group(1).strip()
        desc = re.search(r"^description:\s*(.+)$", fm, re.M)
        self.assertEqual(name, os.path.basename(SKILL_DIR), "frontmatter name must match the dir")
        self.assertTrue(desc and desc.group(1).strip(), "a non-empty description is required")

    def test_mail_setup_referenced_among_linked_reference_files(self):
        linked = re.findall(r"references/([\w.-]+\.md)", _read(SKILL))
        self.assertIn("mail-setup.md", linked, "mail-setup.md must be among the files SKILL.md links")


if __name__ == "__main__":
    unittest.main()
