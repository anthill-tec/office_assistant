"""CR-OA-022 §S1 — opt-in per-account `send` capability flag (RED).

Per DN-mail-access.md §Decision 7: "Send is opt-in per account (read-only
accounts stay read-only); `mail-auth` records a `send`-capability flag, and
the send verbs refuse a non-send-capable account." None of this exists yet:

  - `vidushi_oa.mail.accounts.add_account()` has the CR-OA-020 fixed 5-key
    signature (`name, provider, address, secret_ref, auth_mode="password"`) —
    no `send` parameter, so passing one raises `TypeError` today.
  - `voa mail-auth` has no `--send` flag — passing it fails at argparse.
  - There is no send-gate helper yet at all: pinning
    `vidushi_oa.mail.send_gate.ensure_send_capable(entry: dict) -> None`,
    which raises `PermissionError` (message contains "send") for an entry
    without `entry["send"] is True`, and returns quietly (no exception) for a
    send-capable entry. This is a proposed shape for GREEN to implement — the
    real send verbs (`mail-send`, §S3) will call it before dispatching.

NOTE for GREEN: the CR-OA-020 mail-auth/doctor test
(`tests/test_cr_oa_020_mail_auth_doctor.py`,
`test_mail_auth_stdin_mode_persists_reference_only_and_leaks_no_secret`)
asserts the persisted entry's key set is EXACTLY
`{"name", "provider", "address", "secret_ref", "auth_mode"}`. Adding a `send`
key to every entry (even a default `False`) will need that assertion widened
to include `"send"` — flagged here rather than edited, per this cycle's scope
(RED test-writing only, no edits to unrelated pre-existing tests).
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from vidushi_oa.mail import accounts

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STORE = os.path.join(ROOT, "scripts", "store.py")


class AddAccountSendFlagTest(unittest.TestCase):
    """§S1 AC groundwork: `add_account` must accept and persist a `send` flag."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="oa-cr022-send-")
        self.path = os.path.join(self.tmp, "accounts.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_add_account_persists_send_true_when_requested(self):
        entry = accounts.add_account(
            "gmail:send@x.com", "gmail", "send@x.com", "ref1",
            auth_mode="password", send=True, path=self.path,
        )

        self.assertIs(entry.get("send"), True)
        stored = accounts.load_accounts(self.path)
        self.assertIs(stored[0].get("send"), True)

    def test_add_account_defaults_send_false_when_not_specified(self):
        entry = accounts.add_account(
            "gmail:readonly@x.com", "gmail", "readonly@x.com", "ref2",
            path=self.path,
        )

        self.assertIs(entry.get("send"), False,
                       "a mail-auth entry with no explicit send flag must default "
                       "to non-send-capable (False), not be silently send-capable")

    def test_add_account_can_rotate_an_entry_from_non_send_to_send_capable(self):
        accounts.add_account(
            "gmail:rotate@x.com", "gmail", "rotate@x.com", "ref3", path=self.path,
        )

        updated = accounts.add_account(
            "gmail:rotate@x.com", "gmail", "rotate@x.com", "ref3",
            send=True, path=self.path,
        )

        self.assertIs(updated.get("send"), True)
        stored = accounts.load_accounts(self.path)
        self.assertEqual(len(stored), 1, "re-registering the same name must update in place")
        self.assertIs(stored[0].get("send"), True)


class MailAuthCliSendFlagTest(unittest.TestCase):
    """§S1 AC: `mail-auth --send` records a send-capable account entry;
    `mail-auth` without `--send` records a non-send-capable (default) entry."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="oa-cr022-cli-send-")
        self.accounts_path = os.path.join(self.tmp, "accounts.json")
        self.env = dict(os.environ)
        self.env["VIDUSHI_MAIL_CONFIG"] = self.accounts_path
        self.env["VIDUSHI_FORMAT"] = "json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, args):
        return subprocess.run([sys.executable, STORE, *args],
                               capture_output=True, text=True, env=self.env)

    def test_mail_auth_with_send_flag_persists_a_send_capable_entry(self):
        r = self._run(["mail-auth", "--provider", "gmail", "--address", "s@x.com",
                        "--secret-ref", "vidushi-oa/gmail:s@x.com", "--send"])

        self.assertEqual(r.returncode, 0,
                          f"mail-auth --send must be a recognised flag; stderr={r.stderr!r}")
        with open(self.accounts_path, encoding="utf-8") as f:
            entries = json.load(f)
        self.assertEqual(len(entries), 1)
        self.assertIs(entries[0].get("send"), True)

    def test_mail_auth_without_send_flag_persists_a_non_send_capable_entry(self):
        r = self._run(["mail-auth", "--provider", "gmail", "--address", "r@x.com",
                        "--secret-ref", "vidushi-oa/gmail:r@x.com"])

        self.assertEqual(r.returncode, 0, f"stderr={r.stderr!r}")
        with open(self.accounts_path, encoding="utf-8") as f:
            entries = json.load(f)
        self.assertEqual(len(entries), 1)
        self.assertIs(entries[0].get("send"), False,
                       "mail-auth without --send must persist a non-send-capable entry, "
                       "not omit the field or default it to send-capable")


class SendGateRefusesNonSendCapableAccountTest(unittest.TestCase):
    """§S1 AC: a send-path call against an account lacking the `send`
    capability flag is refused with a structured error naming "send"; a
    send-capable account is allowed through."""

    def test_non_send_capable_entry_is_refused_with_send_in_the_error(self):
        from vidushi_oa.mail.send_gate import ensure_send_capable

        entry = {"name": "gmail:x@x.com", "provider": "gmail", "address": "x@x.com",
                  "secret_ref": "ref", "auth_mode": "password", "send": False}

        with self.assertRaises(PermissionError) as ctx:
            ensure_send_capable(entry)
        self.assertIn("send", str(ctx.exception).lower())

    def test_entry_missing_the_send_key_entirely_is_also_refused(self):
        from vidushi_oa.mail.send_gate import ensure_send_capable

        entry = {"name": "gmail:legacy@x.com", "provider": "gmail",
                  "address": "legacy@x.com", "secret_ref": "ref",
                  "auth_mode": "password"}

        with self.assertRaises(PermissionError) as ctx:
            ensure_send_capable(entry)
        self.assertIn("send", str(ctx.exception).lower())

    def test_send_capable_entry_is_allowed_through_without_raising(self):
        from vidushi_oa.mail.send_gate import ensure_send_capable

        entry = {"name": "gmail:ok@x.com", "provider": "gmail", "address": "ok@x.com",
                  "secret_ref": "ref", "auth_mode": "password", "send": True}

        try:
            ensure_send_capable(entry)
        except PermissionError:
            self.fail("a send-capable account entry must not be refused")


if __name__ == "__main__":
    unittest.main()
