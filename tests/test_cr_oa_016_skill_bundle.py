"""CR-OA-016 — the unified `vidushi-oa` skill bundle is complete + install-ready.

Encodes the CR's grep/validate acceptance gates as a durable regression guard:

  §S1 support domain on the shared lifecycle (no legacy cases enum; case actions named);
  §S2 purchase domain wired to the `orders` store (no placeholder; names orders + delivery-sweep);
  §S3 first-class Insurance section (renew-registration / due-sweep / product_id);
  §S4 five references/ files, each linked from SKILL.md; restored content greps;
  §S5 CLAUDE.md + README.md present the unified skill + the replacement path;
  §S6 README.md + scripts/README.md document both install paths + the license/PyPI gate.

The authoritative shape gate is the official `agentskills validate skills/vidushi-oa`
(`pip install skills-ref`), wrapped by the release gate `~/.claude/scripts/skill-release-gate.py`
(phase 1) and confirmed passing on this bundle. This unit test additionally runs a fast STRUCTURAL
check (name == dir, frontmatter, SKILL.md + references present) that needs no network or extra
install, so bundle drift is caught in the everyday suite too.
"""
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SKILL_DIR = os.path.join(ROOT, "skills", "vidushi-oa")
SKILL = os.path.join(SKILL_DIR, "SKILL.md")
REFS = os.path.join(SKILL_DIR, "references")
REF_FILES = ["search-recipes.md", "carriers-and-customs.md", "subscription-taxonomy.md",
             "calendar-reminders.md", "report-templates.md"]


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class StructuralValidationTest(unittest.TestCase):
    """§S4/§S6 — flat-layout vercel/skills bundle (fast structural proxy for the
    authoritative `agentskills validate` gate, which the release gate runs)."""

    def test_frontmatter_name_matches_dir_and_description_present(self):
        txt = _read(SKILL)
        m = re.match(r"^---\n(.*?)\n---\n", txt, re.S)
        self.assertIsNotNone(m, "SKILL.md must open with YAML frontmatter")
        fm = m.group(1)
        name = re.search(r"^name:\s*(.+)$", fm, re.M).group(1).strip()
        desc = re.search(r"^description:\s*(.+)$", fm, re.M)
        self.assertEqual(name, os.path.basename(SKILL_DIR), "frontmatter name must match the dir")
        self.assertTrue(desc and desc.group(1).strip(), "a non-empty description is required")

    def test_five_reference_files_exist_and_are_linked(self):
        for fn in REF_FILES:
            self.assertTrue(os.path.isfile(os.path.join(REFS, fn)), f"missing references/{fn}")
        body = _read(SKILL)
        for fn in REF_FILES:
            self.assertIn(f"references/{fn}", body, f"SKILL.md must link references/{fn}")
        # no manifest file is required by the flat layout
        self.assertFalse(os.path.exists(os.path.join(SKILL_DIR, "skill.json")))


class SupportDomainTest(unittest.TestCase):
    """§S1."""

    def test_no_legacy_cases_status_enum(self):
        body = _read(SKILL)
        self.assertEqual(
            len(re.findall(r"awaiting_support|rma_issued|in_repair|status[\"': ]+open", body, re.I)),
            0, "the legacy cases status enum must be gone",
        )

    def test_case_action_set_named(self):
        self.assertIn("raise-ticket · rma-issue · ship-back · repair · replace · resolution-confirm",
                      _read(SKILL))


class PurchaseDomainTest(unittest.TestCase):
    """§S2."""

    def test_no_placeholder_and_orders_store_wired(self):
        body = _read(SKILL)
        self.assertNotIn("store type via order tracking", body)
        self.assertIn("store type `orders`", body)
        self.assertIn("delivery-sweep", body)


class InsuranceDomainTest(unittest.TestCase):
    """§S3."""

    def _insurance_section(self):
        body = _read(SKILL)
        m = re.search(r"^### Insurance.*?(?=^### |\Z)", body, re.S | re.M)
        self.assertIsNotNone(m, "a dedicated '### Insurance' section is required")
        return m.group(0)

    def test_insurance_section_names_regulatory_renewal(self):
        sec = self._insurance_section()
        for token in ("renew-registration", "due-sweep", "product_id"):
            self.assertIn(token, sec, f"Insurance section must mention {token}")


class RestoredContentTest(unittest.TestCase):
    """§S4 restored-content greps."""

    def test_carrier_and_calendar_content_present(self):
        carriers = _read(os.path.join(REFS, "carriers-and-customs.md"))
        self.assertIn("Delhivery", carriers)
        cal = _read(os.path.join(REFS, "calendar-reminders.md"))
        self.assertIn("create_event", cal)
        self.assertIn("compose_event", cal)  # the caveat text
        taxo = _read(os.path.join(REFS, "subscription-taxonomy.md"))
        self.assertIn("finance/bank", taxo)
        self.assertIn("security/password-manager", taxo)


class RosterAndInstallDocsTest(unittest.TestCase):
    """§S5 + §S6 — roster supersession and the two install paths."""

    def test_claude_md_presents_unified_skill_and_replacement_path(self):
        claude = _read(os.path.join(ROOT, "CLAUDE.md"))
        self.assertIn("vidushi-oa", claude)
        self.assertRegex(claude, r"supersed|replace")
        self.assertIn("inbox-analyst", claude)

    def test_readme_documents_both_install_paths_and_the_gate(self):
        readme = _read(os.path.join(ROOT, "README.md"))
        self.assertIn("npx skills add", readme)            # skill install (local + public)
        self.assertIn("pip install vidushi-oa", readme)    # engine install
        self.assertRegex(readme, r"PyPI|license")          # names the public-path gate

    def test_scripts_readme_documents_install(self):
        sr = _read(os.path.join(ROOT, "scripts", "README.md"))
        self.assertIn("npx skills add", sr)


if __name__ == "__main__":
    unittest.main()
