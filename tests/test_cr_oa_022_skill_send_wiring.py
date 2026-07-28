"""Doc/skill ↔ engine consistency guard for the 1.1.1 send feature.

The engine's draft-then-confirm verbs (`mail-draft`/`mail-reply`/`mail-send`) shipped
under CR-OA-022, but their CONSUMER side — the unified skill — was originally punted to
"a later skill-revision CR", which is how 1.1.1 nearly shipped an engine capability no
role knew how to drive. This module is the durable guard for the re-sync:

  §A `skills/vidushi-oa/SKILL.md` "Mailboxes & search" documents the three send verbs
     and states the two-step is enforced by the engine (not merely a convention).
  §B the Support domain drives `mail-draft`/`mail-reply` -> show the user ->
     `mail-send` on an explicit confirmation.
  §C the Safety-contract "Draft-then-confirm" bullet points at those engine verbs.
  §D DOC->CODE: every `voa mail-*` flag the skill instructs an agent to type actually
     exists on the shipped CLI (checked against the real `--help`, not a grep of the
     source) — so a renamed/removed flag can never leave the skill teaching a command
     the engine would reject.
  §E `docs/research/DN-mail-access.md` carries the dated "Decision 7 — as-built
     refinement" subsection, its claimed mechanisms are actually present in the
     shipped modules, and it keeps the accepted-gap note that a real-provider E2E
     tier is still owed.

Structural/grep style on purpose, mirroring `test_cr_oa_021_skill_mail_verbs.py`: fast
and hermetic apart from one `--help` subprocess per verb.
"""
import os
import re
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SKILL = os.path.join(ROOT, "skills", "vidushi-oa", "SKILL.md")
DN = os.path.join(ROOT, "docs", "research", "DN-mail-access.md")
STORE = os.path.join(ROOT, "scripts", "store.py")

SEND_VERBS = ("mail-draft", "mail-reply", "mail-send")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _section(body, heading_pattern, level=2):
    """Slice a heading's section up to the next heading of the SAME level or EOF.

    `heading_pattern` matches within the heading LINE only (`[^\\n]*`), so a
    DOTALL `.*` can never swallow intervening sections.
    """
    hashes = "#" * level
    return re.search(rf"^{hashes} [^\n]*" + heading_pattern + rf"[^\n]*\n.*?(?=^{hashes} |\Z)",
                     body, re.S | re.M)


def _code_spans(text, verb):
    """Inline-code spans in `text` that spell an actual `voa <verb> …` command.

    Flags are read ONLY from these spans: the skill wraps prose across lines, so a
    line-oriented scan would mis-attribute a `--draft` from a neighbouring
    `mail-send` span to the `mail-reply` that happens to start the same line.
    """
    return [m.group(1) for m in re.finditer(r"`([^`\n]+)`", text)
            if re.match(rf"(?:voa\s+)?{verb}\b", m.group(1).strip())]


class MailboxesSectionDocumentsSendVerbsTest(unittest.TestCase):
    """§A — 'Mailboxes & search' documents the engine-enforced send two-step."""

    def setUp(self):
        m = _section(_read(SKILL), r"Mailboxes & search")
        self.assertIsNotNone(m, "a '## Mailboxes & search' section is required")
        self.section = m.group(0)

    def test_all_three_send_verbs_documented(self):
        for verb in SEND_VERBS:
            self.assertIn(verb, self.section,
                          f"'Mailboxes & search' must document `voa {verb}`")

    def test_states_the_two_step_is_engine_enforced(self):
        self.assertRegex(
            self.section.lower(), r"enforced in the engine|engine-enforced|enforced at the engine",
            "the section must say draft-then-confirm is enforced by the ENGINE, not "
            "merely an agent convention",
        )

    def test_draft_verbs_stated_to_perform_no_send(self):
        self.assertRegex(
            self.section.lower(), r"no network send|zero send|performs \*\*no\*\* network send",
            "the draft verbs must be documented as performing no network send",
        )

    def test_send_is_documented_as_per_account_opt_in(self):
        self.assertRegex(
            self.section.lower(), r"opt-in per account|per-account opt-in",
            "send capability is opt-in per account (`mail-auth --send`) — the skill "
            "must say so, or an agent will read a refusal as a bug",
        )

    def test_send_verb_targets_one_identified_draft(self):
        self.assertRegex(
            self.section, r"mail-send[^\n]*--draft",
            "`mail-send` must be documented as dispatching an identified `--draft`",
        )


class SupportDomainDrivesTheSendVerbsTest(unittest.TestCase):
    """§B — the Support domain wires draft-then-confirm to the engine verbs."""

    def setUp(self):
        m = _section(_read(SKILL), r"Support", level=3)
        self.assertIsNotNone(m, "a Support domain section is required")
        self.section = m.group(0)

    def test_support_drafts_via_mail_draft_or_mail_reply(self):
        self.assertIn("mail-draft", self.section,
                      "Support correspondence must be drafted with `voa mail-draft`")
        self.assertIn("mail-reply", self.section,
                      "threading onto a vendor message must use `voa mail-reply`")

    def test_support_dispatches_via_mail_send_on_explicit_confirmation(self):
        self.assertIn("mail-send", self.section,
                      "Support must dispatch through `voa mail-send`")
        self.assertRegex(
            self.section.lower(), r"explicit",
            "the send step must be gated on the user's EXPLICIT confirmation",
        )
        self.assertRegex(self.section.lower(), r"never\s+auto-send",
                         "the never-auto-send invariant must survive the rewrite")

    def test_support_links_the_correspondence_trail(self):
        self.assertRegex(
            self.section, r"--case",
            "the draft must be linked to the case row (`--case <id>`) so the send "
            "records the correspondence trail",
        )


class SafetyContractPointsAtTheEngineTest(unittest.TestCase):
    """§C — the safety-contract bullet names the engine verbs."""

    def setUp(self):
        body = _read(SKILL)
        m = re.search(r"^-\s+\*\*Draft-then-confirm.*?(?=^-\s+\*\*)", body, re.S | re.M)
        self.assertIsNotNone(m, "a '**Draft-then-confirm:**' safety bullet is required")
        self.bullet = m.group(0)

    def test_bullet_names_the_draft_and_send_verbs(self):
        for verb in SEND_VERBS:
            self.assertIn(verb, self.bullet,
                          f"the draft-then-confirm safety bullet must name `{verb}`")

    def test_bullet_asserts_there_is_no_other_send_path(self):
        self.assertRegex(
            self.bullet.lower(), r"no other send path",
            "the bullet must state the engine has no other send path",
        )
        self.assertIn("Never auto-send", self.bullet)


class SkillFlagsExistOnTheShippedCliTest(unittest.TestCase):
    """§D — doc->code: every flag the skill types is a real flag on the real CLI."""

    @classmethod
    def setUpClass(cls):
        cls.help = {}
        for verb in SEND_VERBS:
            result = subprocess.run([sys.executable, STORE, verb, "--help"],
                                    capture_output=True, text=True, timeout=120)
            assert result.returncode == 0, f"{verb} --help failed: {result.stderr}"
            cls.help[verb] = result.stdout
        cls.skill = _read(SKILL)

    def _documented_flags(self, verb):
        """Flags the skill types inside an actual `voa <verb> …` command span."""
        flags = set()
        for span in _code_spans(self.skill, verb):
            flags |= set(re.findall(r"--[a-z][a-z-]*", span))
        return flags

    def test_documented_flags_are_real_cli_flags(self):
        for verb in SEND_VERBS:
            documented = self._documented_flags(verb)
            self.assertTrue(documented, f"the skill documents no flags for {verb}")
            for flag in documented:
                self.assertIn(
                    flag, self.help[verb],
                    f"skill instructs `voa {verb} {flag}` but the shipped CLI's "
                    f"--help does not offer {flag}",
                )

    def test_required_flags_of_each_verb_are_documented(self):
        """Anything argparse marks required must appear in the skill, or an agent
        following the skill writes a command the CLI rejects."""
        for verb in SEND_VERBS:
            usage = re.search(r"usage:.*?(?=\n\noptions:)", self.help[verb], re.S).group(0)
            required = set(re.findall(r"(?<![\[\w])--([a-z][a-z-]*)", usage))
            documented = self._documented_flags(verb)
            for flag in sorted(required):
                self.assertIn(
                    f"--{flag}", documented,
                    f"`voa {verb}` requires --{flag} but the skill never shows it",
                )

    def test_every_optional_flag_named_in_the_send_block_is_real(self):
        """The optional flags the send block advertises alongside the commands
        (`--cc`, `--attach`, the FK flags) must belong to one of the send verbs."""
        m = re.search(r"Sending is draft-then-confirm.*?(?=^The user files mail|\Z)",
                      self.skill, re.S | re.M)
        self.assertIsNotNone(m, "the 'Sending is draft-then-confirm' block is required")
        named = set(re.findall(r"`[^`\n]*?(--[a-z][a-z-]*)[^`\n]*?`", m.group(0)))
        named |= {f for span in re.findall(r"`([^`\n]+)`", m.group(0))
                  for f in re.findall(r"--[a-z][a-z-]*", span)}
        self.assertTrue(named, "the send block names no flags at all")
        for flag in sorted(named):
            self.assertTrue(
                any(flag in self.help[v] for v in SEND_VERBS),
                f"the send block advertises {flag}, which no send verb accepts",
            )


class DecisionSevenAsBuiltTest(unittest.TestCase):
    """§E — the DN's as-built re-sync exists and matches the shipped modules."""

    def setUp(self):
        self.dn = _read(DN)
        m = _section(self.dn, r"Decision 7 — as-built refinement", level=3)
        self.assertIsNotNone(
            m, "DN-mail-access.md must carry a '### Decision 7 — as-built refinement' "
               "subsection re-syncing the design to the shipped code")
        self.section = m.group(0)

    def test_subsection_is_dated(self):
        self.assertRegex(re.split(r"\n", self.section)[0], r"\d{4}-\d{2}-\d{2}",
                         "the as-built subsection must carry its re-sync date")

    def test_records_that_cr_oa_022_is_not_amended(self):
        self.assertRegex(
            self.section, r"CR-OA-022 is \*\*not\*\* amended|not\*\* amended",
            "the subsection must state the completed CR-OA-022 record is not amended",
        )

    def test_claimed_mechanisms_exist_in_the_shipped_code(self):
        """Each as-built claim is tied to the token that proves it in the engine."""
        claims = {
            # (doc token the DN claims, module, token that must exist in it)
            "Email/import": ("mail/jmap.py", "Email/import"),
            "uploadUrl": ("mail/jmap.py", "uploadUrl"),
            "onSuccessUpdateEmail": ("mail/jmap.py", "onSuccessUpdateEmail"),
            "policy.SMTP": ("mail/compose.py", "policy.SMTP"),
            "Message-ID": ("mail/compose.py", "Message-ID"),
            # The DN names `smtp.auth("XOAUTH2", …)` specifically — assert the CALL,
            # not a passing mention, so a regression to `login` with an empty
            # password cannot leave the claim looking satisfied.
            'smtp.auth("XOAUTH2"': ("mail/xoauth2.py", 'smtp.auth("XOAUTH2"'),
            "EXPUNGE": ("mail/imap.py", 'uid("EXPUNGE"'),
        }
        for doc_token, (rel, code_token) in claims.items():
            self.assertIn(doc_token, self.section,
                          f"the as-built subsection must document {doc_token!r}")
            src = _read(os.path.join(ROOT, "vidushi_oa", *rel.split("/")))
            self.assertIn(code_token, src,
                          f"DN claims {doc_token!r} but vidushi_oa/{rel} has no "
                          f"{code_token!r} — doc and code have drifted")

    def test_special_use_resolution_claim_matches_code(self):
        self.assertRegex(self.section, r"RFC ?6154",
                         "the DN must document RFC 6154 special-use folder resolution")
        src = _read(os.path.join(ROOT, "vidushi_oa", "mail", "imap.py"))
        self.assertIn("6154", src, "imap.py must implement the RFC 6154 resolution")

    def test_cc_is_covered_by_the_verified_recipient_guard(self):
        self.assertRegex(self.section, r"To \*and\* Cc|To and Cc",
                         "the DN must record that the guard covers Cc as well as To")
        cli_src = _read(os.path.join(ROOT, "vidushi_oa", "_cli.py"))
        self.assertEqual(
            2, cli_src.count("_verified_recipient_or_exit(a."),
            "cmd_mail_draft must run the verified-recipient guard over BOTH a.to and "
            "a.cc, as the DN's as-built subsection claims",
        )

    def test_records_the_still_owed_real_provider_e2e_gap(self):
        self.assertRegex(
            self.section, r"E2E",
            "the accepted gap — a real-provider E2E validation tier is still owed — "
            "must stay recorded; the in-process fakes are what let the round-1 empty-"
            "draft bug pass green",
        )

    def test_skill_consequence_no_longer_defers_the_wiring(self):
        m = re.search(r"^-\s+\*\*The skill changes\*\*.*?(?=^-\s+\*\*)", self.dn,
                      re.S | re.M)
        self.assertIsNotNone(m, "the 'The skill changes' consequence bullet is required")
        bullet = m.group(0)
        self.assertNotRegex(
            bullet, r"a later skill-revision CR\)",
            "the consequence bullet must no longer punt the skill wiring to a later "
            "CR — that punt is what shipped the engine verbs unwired",
        )
        for verb in SEND_VERBS:
            self.assertIn(verb, bullet,
                          f"the consequence bullet must name `{verb}` as landing WITH "
                          f"the send feature")


if __name__ == "__main__":
    unittest.main()
