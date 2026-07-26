"""CR-OA-012 — unified `vidushi-oa` skill (`skills/vidushi-oa/SKILL.md`).

Verifies the NEW-contract ACs before the consolidated skill file lands:

  §S1 — one skill file covering all six role-domains (subscriptions,
        purchases/deliveries+customs, invoices, warranties, product
        catalogue, support cases) plus a read-only `deep-sweep` MODE that
        replaces the separate `inbox-analyst` agent (a skill mode ports
        across harnesses; a subagent does not), and the safety contract
        (phishing/customs awareness, draft-then-confirm, verified-contacts-
        only) surviving consolidation.

  §S2 — the `vidushi-oa` engine (`uv tool install vidushi-oa` + `voa setup`)
        declared as a prerequisite, the store driven exclusively through
        the `voa` CLI (never raw Mongo, never an MCP server), and a
        harness-agnostic front-matter block / portability statement so the
        skill is not tied to `~/.claude/`.

Today `skills/vidushi-oa/SKILL.md` does NOT exist at all. Every test in
this module MUST fail until CR-OA-012's GREEN phase authors it. Each test
guards the missing-file case with a plain `assertTrue(os.path.exists(...))`
so it fails cleanly (a normal assertion failure) rather than surfacing an
uncaught `FileNotFoundError`.
"""
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SKILL_PATH = os.path.join(ROOT, "skills", "vidushi-oa", "SKILL.md")


def _front_matter_block(text):
    """Return the raw front-matter body (the lines between the two leading
    `---` delimiters), or None if the file doesn't open with `---` or never
    closes the block."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "\n".join(lines[1:i])
    return None


def _body_text(text):
    """Best-effort slice of the body after the closing front-matter `---`.
    Falls back to the whole file when the front matter isn't well-formed,
    so the content/domain tests (T2-T6) stay independent of T1's
    structural front-matter checks."""
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for i, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                return "\n".join(lines[i + 1:])
    return text


class _SkillFileTestCase(unittest.TestCase):
    """Shared existence guard + the 'named only as an optional Claude-Code
    detail' negative check reused by the deep-sweep (T3) and harness (T6)
    groups."""

    _OPTIONAL_MARKERS = ("optional", "claude code", "under the hood")

    def _read_skill(self):
        self.assertTrue(
            os.path.exists(SKILL_PATH),
            f"expected {SKILL_PATH} to exist — CR-OA-012 GREEN must author it",
        )
        with open(SKILL_PATH, "r", encoding="utf-8") as fh:
            return fh.read()

    def _assert_optional_detail_only(self, body, keyword):
        """Every occurrence of `keyword` must co-occur (same line or an
        adjacent line) with one of the optional-Claude-Code-detail markers.
        If `keyword` never appears at all, there is nothing to violate."""
        lines = body.splitlines()
        violations = []
        for i, line in enumerate(lines):
            if keyword.lower() in line.lower():
                window = " ".join(
                    lines[max(0, i - 1): min(len(lines), i + 2)]
                ).lower()
                if not any(marker in window for marker in self._OPTIONAL_MARKERS):
                    violations.append((i + 1, line.strip()))
        self.assertEqual(
            violations, [],
            f"found {keyword!r} mentioned without an 'optional'/'Claude Code'/"
            f"'under the hood' qualifier nearby (line, text): {violations} — "
            f"{keyword!r} may only be named as an optional Claude-Code detail, "
            f"never a required dependency",
        )


class SkillFrontMatterTest(_SkillFileTestCase):
    """T1 — file exists; YAML front matter delimited by leading `---`...`---`;
    contains `name: vidushi-oa` and a non-empty `description:`."""

    def test_skill_file_exists_and_is_readable_utf8(self):
        self.assertTrue(os.path.exists(SKILL_PATH), f"expected {SKILL_PATH} to exist")
        with open(SKILL_PATH, "r", encoding="utf-8") as fh:
            content = fh.read()
        self.assertGreater(len(content), 0, "SKILL.md exists but is empty")

    def test_front_matter_block_is_delimited_by_leading_and_closing_triple_dash(self):
        text = self._read_skill()
        lines = text.splitlines()
        self.assertTrue(bool(lines), "SKILL.md is empty, cannot find front matter")
        self.assertEqual(
            lines[0].strip(), "---",
            f"expected SKILL.md to open with a `---` front-matter delimiter, "
            f"first line was: {lines[0]!r}",
        )
        fm = _front_matter_block(text)
        self.assertIsNotNone(
            fm, "expected a closing `---` delimiter terminating the front-matter block",
        )

    def test_front_matter_declares_name_vidushi_oa(self):
        text = self._read_skill()
        fm = _front_matter_block(text)
        self.assertIsNotNone(fm, "expected a well-formed `---`...`---` front-matter block")
        match = re.search(r'^name:\s*["\']?vidushi-oa["\']?\s*$', fm, re.MULTILINE)
        self.assertIsNotNone(
            match,
            f"expected front matter to declare `name: vidushi-oa` exactly, "
            f"front matter was:\n{fm}",
        )

    def test_front_matter_declares_non_empty_description(self):
        text = self._read_skill()
        fm = _front_matter_block(text)
        self.assertIsNotNone(fm, "expected a well-formed `---`...`---` front-matter block")
        match = re.search(r'^description:\s*(.+)$', fm, re.MULTILINE)
        self.assertIsNotNone(
            match, f"expected front matter to declare a `description:` key, "
            f"front matter was:\n{fm}",
        )
        value = match.group(1).strip().strip('"\'')
        self.assertGreater(len(value), 0, "expected a non-empty `description:` value in front matter")


class SkillSixDomainsTest(_SkillFileTestCase):
    """T2 — §S1: the body references all six consolidated domains. Asserted
    individually so a miss names the exact domain."""

    def test_body_mentions_subscription_domain(self):
        body = _body_text(self._read_skill())
        self.assertIn("subscription", body.lower(),
                       "expected the body to reference the subscriptions domain (keyword: 'subscription')")

    def test_body_mentions_purchase_domain(self):
        body = _body_text(self._read_skill())
        self.assertIn("purchase", body.lower(),
                       "expected the body to reference the purchases/deliveries domain (keyword: 'purchase')")

    def test_body_mentions_invoice_domain(self):
        body = _body_text(self._read_skill())
        self.assertIn("invoice", body.lower(),
                       "expected the body to reference the invoices domain (keyword: 'invoice')")

    def test_body_mentions_warranty_domain(self):
        body = _body_text(self._read_skill())
        self.assertIn("warranty", body.lower(),
                       "expected the body to reference the warranties domain (keyword: 'warranty')")

    def test_body_mentions_product_domain(self):
        body = _body_text(self._read_skill())
        self.assertIn("product", body.lower(),
                       "expected the body to reference the product-catalogue domain (keyword: 'product')")

    def test_body_mentions_support_domain(self):
        body = _body_text(self._read_skill())
        self.assertIn("support", body.lower(),
                       "expected the body to reference the support-case domain (keyword: 'support')")


class SkillDeepSweepModeTest(_SkillFileTestCase):
    """T3 — §S1: `deep-sweep` is documented as a MODE (not a separate agent).
    `inbox-analyst`, if named at all, may only be an optional Claude-Code
    detail."""

    def test_body_documents_deep_sweep_as_a_mode(self):
        body = _body_text(self._read_skill())
        lower = body.lower()
        occurrences = [m.start() for m in re.finditer(re.escape("deep-sweep"), lower)]
        self.assertTrue(occurrences, "expected the body to mention a `deep-sweep` mode")
        window_hits = [
            lower[max(0, idx - 80): idx + len("deep-sweep") + 80]
            for idx in occurrences
        ]
        satisfied = any("mode" in window for window in window_hits)
        self.assertTrue(
            satisfied,
            f"expected the word 'mode' within ~80 chars of at least one `deep-sweep` "
            f"occurrence (deep-sweep must be presented as a MODE), windows checked: {window_hits}",
        )

    def test_inbox_analyst_named_only_as_optional_detail_if_present(self):
        body = _body_text(self._read_skill())
        self._assert_optional_detail_only(body, "inbox-analyst")


class SkillSafetyContractTest(_SkillFileTestCase):
    """T4 — §S1: the phishing/customs safety contract, draft-then-confirm,
    and verified-contacts-only rules all survive consolidation."""

    def test_body_contains_phishing_rule(self):
        body = _body_text(self._read_skill())
        self.assertIn("phishing", body.lower(),
                       "expected the safety contract's phishing rule to survive consolidation")

    def test_body_contains_customs_handling(self):
        body = _body_text(self._read_skill())
        self.assertIn("customs", body.lower(),
                       "expected the safety contract's customs handling to survive consolidation")

    def test_body_contains_literal_draft_then_confirm(self):
        body = _body_text(self._read_skill())
        match = re.search(r'draft-then-confirm', body, re.IGNORECASE)
        self.assertIsNotNone(
            match,
            "expected the literal, hyphenated phrase 'draft-then-confirm' to survive "
            "consolidation (a de-hyphenated paraphrase does not satisfy this AC)",
        )

    def test_body_contains_verified_contacts_rule(self):
        body = _body_text(self._read_skill())
        lines = body.lower().splitlines()
        matching = [ln for ln in lines if "verified" in ln and "contact" in ln]
        self.assertTrue(
            matching,
            "expected a line containing both 'verified' and 'contact' "
            "(the verified-contacts-only rule)",
        )


class SkillEnginePrerequisiteTest(_SkillFileTestCase):
    """T5 — §S2: the `vidushi-oa` engine is named as a prerequisite
    (`uv tool install vidushi-oa` + `voa setup`); the store is driven via the
    `voa` CLI, never raw Mongo and never a hard MCP-server requirement."""

    def test_body_declares_uv_tool_install_vidushi_oa_prerequisite(self):
        body = _body_text(self._read_skill())
        self.assertIn(
            "uv tool install vidushi-oa", body.lower(),
            "expected the body to name `uv tool install vidushi-oa` as the engine prerequisite",
        )

    def test_body_declares_voa_setup_prerequisite(self):
        body = _body_text(self._read_skill())
        self.assertIn(
            "voa setup", body.lower(),
            "expected the body to name `voa setup` as part of the install order",
        )

    def test_body_uses_voa_as_a_cli_command(self):
        body = _body_text(self._read_skill())
        match = re.search(r'`voa\b', body)
        self.assertIsNotNone(
            match,
            "expected the body to show `voa` used as a command (e.g. a code span "
            "like `voa setup` or `voa query`), word-boundary after 'voa'",
        )

    def test_body_states_store_accessed_via_cli_never_raw_mongo(self):
        body = _body_text(self._read_skill())
        lower = body.lower()
        mongo_positions = [m.start() for m in re.finditer("mongo", lower)]
        self.assertTrue(
            mongo_positions,
            "expected the body to mention Mongo at all, to state the CLI-only access rule against it",
        )
        markers = ("never", "not ", "only through", "exclusively", "instead of", "bypass")
        satisfied = any(
            any(marker in lower[max(0, idx - 100): idx + 100] for marker in markers)
            for idx in mongo_positions
        )
        self.assertTrue(
            satisfied,
            "expected a nearby restriction ('never'/'only through'/'exclusively'/'bypass'/"
            "'instead of') around a 'mongo' mention, stating the store is accessed via "
            "the CLI, never raw Mongo",
        )

    def test_no_hard_mcp_server_requirement_if_mentioned(self):
        body = _body_text(self._read_skill())
        lines = body.splitlines()
        disclaimers = ("not", "no ", "instead of")
        violations = []
        for i, line in enumerate(lines):
            if re.search(r'\bMCP\b', line, re.IGNORECASE):
                if not any(d in line.lower() for d in disclaimers):
                    violations.append((i + 1, line.strip()))
        self.assertEqual(
            violations, [],
            f"found 'MCP' mentioned without a not/no/instead-of disclaimer on the same "
            f"line (line, text): {violations} — the skill must not require an MCP server",
        )


class SkillHarnessAgnosticTest(_SkillFileTestCase):
    """T6 — §S2: the skill asserts portability across agentic harnesses, and
    subagents (if named) are only an optional Claude-Code detail."""

    def test_body_declares_harness_agnostic_portability(self):
        body = _body_text(self._read_skill())
        lower = body.lower()
        self.assertIn("harness", lower, "expected the body to discuss harness portability")
        phrases = ("any harness", "harness-agnostic", "any agent")
        self.assertTrue(
            any(p in lower for p in phrases),
            f"expected at least one portability phrase {phrases} alongside 'harness'",
        )

    def test_subagents_named_only_as_optional_detail_if_present(self):
        body = _body_text(self._read_skill())
        self._assert_optional_detail_only(body, "subagent")


if __name__ == "__main__":
    unittest.main()
