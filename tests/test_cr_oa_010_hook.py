"""CR-OA-010 Cycle D — S5/AXI #7 ambient context via a session hook.

Verifies the design in CR-OA-010 Sec S5: a Claude Code `SessionStart` hook
configured in `.claude/settings.json` that runs `store.py` with no
arguments, surfacing the `attention` worklist as ambient context BEFORE
the agent acts (Cycle C already made bare `store.py` print that worklist).

Baseline behaviour TODAY (pre-GREEN):
  - `.claude/settings.json` does not exist at the repo root -> test 1
    (hook config present + well-formed) MUST FAIL.
  - Neither `CLAUDE.md` nor `scripts/README.md` documents the hook -> test
    3 (documented) MUST FAIL.
  - `store.py` (no args) already prints the attention worklist (Cycle C) ->
    test 2 (hook body actually emits the worklist) passes today; kept as a
    guard that the hook's target keeps working.

DATA SAFETY: test 2's subprocess call points `VIDUSHI_DATA_DIR` at an EMPTY
tempdir (never the real repo `data/`) and `VIDUSHI_MONGO_DB` at
`vidushi_oa_test` (never the real DB), dropped/removed in
tearDown. Requires a local mongod on 127.0.0.1:27017 (office_assistant
instance; CR-OA-001).
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import pymongo

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, "scripts")
STORE = os.path.join(SCRIPTS, "store.py")
SETTINGS = os.path.join(ROOT, ".claude", "settings.json")
CLAUDE_MD = os.path.join(ROOT, "CLAUDE.md")
SCRIPTS_README = os.path.join(SCRIPTS, "README.md")

TEST_DB = "vidushi_oa_test"

# Seed one attention-worthy subscription: an OPEN action, matching the
# `$or: [{"actions.status": "OPEN"}, {"status": {"$in": ATTENTION_STATUSES}}]`
# query in store.py's `cmd_attention` (same fixture shape as the Cycle C
# no-arg test in test_cr_oa_010_no_arg.py).
SEED_SUBSCRIPTIONS = [
    {
        "id": "sub_hookflag",
        "provider": "HookAcme",
        "status": "IN_PROGRESS",
        "actions": [
            {"action": "renew-before-lapse", "status": "OPEN", "owner": "user"},
        ],
    },
]

DESTRUCTIVE_VERB_RE = re.compile(r"\b(rm|update|add)\b")


def _load_settings():
    with open(SETTINGS, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _iter_session_start_commands(settings):
    """Yield every command string found under hooks.SessionStart[*].hooks[*]."""
    session_start = settings.get("hooks", {}).get("SessionStart", [])
    for entry in session_start:
        for hook in entry.get("hooks", []):
            yield hook


class HookConfigTest(unittest.TestCase):
    """S5/AXI#7 — `.claude/settings.json` wires a SessionStart hook to store.py."""

    def test_settings_json_exists_and_parses(self):
        self.assertTrue(
            os.path.isfile(SETTINGS),
            f"expected {SETTINGS} to exist (SessionStart hook config)",
        )
        # Must be valid JSON — json.load raises if malformed.
        settings = _load_settings()
        self.assertIsInstance(settings, dict)

    def test_session_start_hook_is_present_and_non_empty(self):
        settings = _load_settings()
        self.assertIn("hooks", settings)
        self.assertIn("SessionStart", settings["hooks"])
        session_start = settings["hooks"]["SessionStart"]
        self.assertIsInstance(session_start, list)
        # POSITIVE: at least one SessionStart entry configured.
        self.assertGreater(len(session_start), 0)

    def test_session_start_hook_command_references_store_py(self):
        settings = _load_settings()
        commands = list(_iter_session_start_commands(settings))

        # POSITIVE: at least one command-type hook exists.
        command_hooks = [h for h in commands if h.get("type") == "command"]
        self.assertGreater(
            len(command_hooks), 0,
            f"expected at least one type=='command' hook under SessionStart, got: {commands!r}",
        )

        matching = [h for h in command_hooks if isinstance(h.get("command"), str) and "store.py" in h["command"]]
        self.assertEqual(
            len(matching), 1,
            f"expected exactly one SessionStart command hook referencing store.py, got: {command_hooks!r}",
        )

        command = matching[0]["command"]
        # NEGATIVE: no-arg invocation must be read-only ambient context —
        # it must not reference a destructive verb (rm/update/add).
        self.assertIsNone(
            DESTRUCTIVE_VERB_RE.search(command),
            f"SessionStart hook command must be read-only, found destructive verb in: {command!r}",
        )


class HookBodyEmitsWorklistTest(unittest.TestCase):
    """The store.py invocation the hook runs actually surfaces the attention worklist."""

    def setUp(self):
        self.data_dir = tempfile.mkdtemp(prefix="oa-cr010d-hook-")
        self.env = dict(os.environ)
        self.env["VIDUSHI_MONGO_DB"] = TEST_DB
        self.env["VIDUSHI_DATA_DIR"] = self.data_dir

        self.client = pymongo.MongoClient("mongodb://127.0.0.1:27017", serverSelectionTimeoutMS=2000)
        self.db = self.client[TEST_DB]
        self.db["subscriptions"].insert_many([dict(row) for row in SEED_SUBSCRIPTIONS])

    def tearDown(self):
        self.client.drop_database(TEST_DB)
        self.client.close()
        shutil.rmtree(self.data_dir, ignore_errors=True)

    def test_hook_target_store_py_no_arg_surfaces_seeded_attention_item(self):
        # Runs the same no-arg invocation the hook's `command` performs
        # (python3 "$CLAUDE_PROJECT_DIR/scripts/store.py"), directly.
        result = subprocess.run(
            [sys.executable, STORE],
            capture_output=True, text=True, env=self.env,
        )

        # POSITIVE: exit 0.
        self.assertEqual(result.returncode, 0, f"stderr: {result.stderr}")

        stdout = result.stdout
        # POSITIVE: the seeded attention item (its id or its OPEN action)
        # is surfaced in the ambient-context output.
        self.assertTrue(
            "sub_hookflag" in stdout or "renew-before-lapse" in stdout,
            f"expected seeded attention item to appear in hook's no-arg output, got: {stdout!r}",
        )
        # NEGATIVE: this is not the argparse usage/error block.
        self.assertFalse(stdout.startswith("usage:"))


class HookDocumentedTest(unittest.TestCase):
    """The SessionStart attention hook is documented for humans/agents to find."""

    def _doc_mentions_hook(self, path):
        if not os.path.isfile(path):
            return False
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read().lower()
        mentions_session_start = "sessionstart" in text
        mentions_hook = "hook" in text
        mentions_attention = "attention" in text
        return (mentions_session_start or mentions_hook) and mentions_attention

    def test_claude_md_or_scripts_readme_documents_the_hook(self):
        claude_md_documents = self._doc_mentions_hook(CLAUDE_MD)
        scripts_readme_documents = self._doc_mentions_hook(SCRIPTS_README)

        # POSITIVE: at least one of the two docs mentions the hook.
        self.assertTrue(
            claude_md_documents or scripts_readme_documents,
            "expected CLAUDE.md or scripts/README.md to document the SessionStart "
            "attention hook (mentioning 'SessionStart' or 'hook', plus 'attention')",
        )


if __name__ == "__main__":
    unittest.main()
