"""CR-OA-023 §S4 — `voa doctor` remediation wizard + `--fix` (RED).

Per docs/changes/CR-OA-023-keyring-primary-os-aware-secret-setup.md §S4 / ACs, `voa
doctor` grows three new things beyond the CR-OA-020 §S7 diagnostic it already reports:

  1. The secret-backend line reports an explicit CONFIRMED marker + an OS-specific fix
     hint when it lands on the last-resort file backend (never a silent "keyring"
     misreport just because the `keyring` module happens to be importable), and the
     output names neither `1password` nor `bitwarden`.
  2. When there is a real gap (Secret Service unwired AND/OR an account whose
     secret_ref does not resolve), doctor emits an ORDERED, machine-readable
     `remediation` plan — one step per gap — where every step carries a boolean
     `human_input` field, and the human-input steps ("enable Secret Service", "run
     mail-auth for <account>") also appear in the AXI `next[]` chain.
  3. `voa doctor --fix` INSTANTIATES the interactive `mail-auth` step for a gap
     account (chaining into it rather than merely printing the raw invocation for the
     user to hand-assemble), and never accepts the secret via a bare CLI arg — only
     via the same hidden-input/stdin path `mail-auth` itself uses (DN §Decision 6).

NONE of this exists yet today:
  - `cmd_doctor` (vidushi_oa/_cli.py) sets `secret_backend = resolver._primary_backend().name`
    directly — a plain string with no `confirmed`/hint fields, and `_primary_backend()`
    only checks whether the `keyring` *module* is importable (`KeyringBackend.available()`
    -> `_keyring is not None`), not whether a provider is actually reachable. So today,
    even with NO Secret-Service provider reachable (a faked `keyring.backends.fail.Keyring`
    that raises `NoKeyringError` on every call), doctor still misreports
    `secret_backend: "keyring"` instead of falling through to `"file"` -- both the
    misreport AND the missing confirmed/hint fields are RED.
  - `cmd_doctor` has no remediation plan at all — no `remediation` key, no `human_input`
    concept anywhere.
  - `doctor` has no `--fix` option (`dr = add_parser("doctor"); read_json(dr); ...` only)
    -> passing `--fix` is an argparse error (exit 2, "unrecognized arguments") before
    `cmd_doctor` ever runs.

Hermetic-environment choices (NO real OS Secret Service, NO live network), matching the
patterns already used by test_cr_oa_020_mail_auth_doctor.py and
test_cr_oa_023_os_aware_setup.py:
  - "Provider absent" is faked deterministically via
    `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring` (ships with `keyring` itself;
    `get_password`/`set_password` unconditionally raise `NoKeyringError` -- no real D-Bus
    session touched).
  - "Provider present / working" is faked via
    `PYTHON_KEYRING_BACKEND=keyrings.alt.file.PlaintextKeyring` (the `[test]` extra's
    file-backed keyring), relocated under a fresh `XDG_DATA_HOME` tempdir so it never
    touches a real OS keyring.
  - Desktop is faked via `XDG_CURRENT_DESKTOP=KDE` to pin the OS-specific hint text.
  - `VIDUSHI_BACKEND=sqlite` + a tmp `VIDUSHI_SQLITE_PATH`, plus tmp `VIDUSHI_MAIL_CONFIG`
    / `VIDUSHI_SECRETS_FILE`, isolate every read/write from the real store and real
    filesystem config.

Design decisions this RED suite PINS for GREEN to match (flagged per the dispatch
brief — no AC mandates the exact field/flag spelling, so these are load-bearing):
  - `secret_backend` stays the EXISTING bare string (`"keyring"`/`"file"`) — unchanged,
    to stay compatible with test_cr_oa_020_mail_auth_doctor.py's
    `self.assertEqual(payload.get("secret_backend"), "file")` assertion. TWO NEW
    top-level keys are added alongside it: `secret_backend_confirmed` (bool) and
    `secret_backend_hint` (str, OS-specific remedy text — expected to reuse
    `vidushi_oa.mail.secrets.keyring_guidance()`).
  - `remediation` is a NEW top-level list of `{"step": <str>, "human_input": <bool>}`
    dicts, ordered so an "enable Secret Service" step (mentioning the literal phrase
    "Secret Service") precedes a "run mail-auth for <account>" step (mentioning the
    literal substrings "mail-auth" and the account's address) when both gaps exist.
    Both required steps carry `human_input: True`. The existing `next[]` envelope key
    must include entries reflecting the same two remediation items (substring match on
    "Secret Service" and on "mail-auth" + the account's address).
  - `--fix` is a bare boolean flag on the `doctor` subparser (no argument), consumed as
    `a.fix`, and its handling INSTANTIATES (executes) the same interactive/stdin secret
    entry `mail-auth` itself uses for each account gap it detects, rather than only
    printing the raw command — proven here by feeding stdin and then resolving the
    account's `secret_ref` afterwards.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STORE = os.path.join(ROOT, "scripts", "store.py")
CLI_SRC = os.path.join(ROOT, "vidushi_oa", "_cli.py")

SENTINEL = "S3NTINEL-FIX-SECRET"
FAIL_KEYRING = "keyring.backends.fail.Keyring"
# A REAL, storable, file-backed keyring from the `keyrings.alt` package (the `[test]`
# extra) -- see test_cr_oa_020_mail_auth_doctor.py for why this (and not the null
# backend) is used to prove "a provider is genuinely reachable".
ALT_FILE_KEYRING = "keyrings.alt.file.PlaintextKeyring"


class _DoctorWizardTestBase(unittest.TestCase):
    """Env isolation for OS/desktop + keyring-provider faking, plus a tmp store,
    accounts file, and secrets file so no real OS keyring / Secret Service / on-disk
    state leaks in or out."""

    ENV_KEYS = (
        "XDG_CURRENT_DESKTOP",
        "PYTHON_KEYRING_BACKEND",
        "XDG_DATA_HOME",
        "VIDUSHI_SECRET_BACKEND",
        "VIDUSHI_SECRETS_FILE",
        "VIDUSHI_MAIL_CONFIG",
        "VIDUSHI_BACKEND",
        "VIDUSHI_SQLITE_PATH",
        "VIDUSHI_FORMAT",
    )

    def setUp(self):
        self._saved_env = {k: os.environ.get(k) for k in self.ENV_KEYS}
        for k in self.ENV_KEYS:
            os.environ.pop(k, None)

        self.tmp = tempfile.mkdtemp(prefix="oa-cr023-s4-")
        self.accounts_path = os.path.join(self.tmp, "accounts.json")
        self.secrets_path = os.path.join(self.tmp, "secrets.json")
        self.sqlite_path = os.path.join(self.tmp, "oa.db")
        self.keyring_home = os.path.join(self.tmp, "keyring-home")
        os.makedirs(self.keyring_home, exist_ok=True)

        self.env = dict(os.environ)
        self.env["VIDUSHI_BACKEND"] = "sqlite"
        self.env["VIDUSHI_SQLITE_PATH"] = self.sqlite_path
        self.env["VIDUSHI_MAIL_CONFIG"] = self.accounts_path
        self.env["VIDUSHI_SECRETS_FILE"] = self.secrets_path

    def tearDown(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, args, fmt=None, env_overrides=None, input_=None):
        env = dict(self.env)
        if fmt:
            env["VIDUSHI_FORMAT"] = fmt
        if env_overrides:
            env.update(env_overrides)
        return subprocess.run(
            [sys.executable, STORE, *args], capture_output=True, text=True,
            env=env, input=input_,
        )

    def _seed_accounts(self, entries):
        with open(self.accounts_path, "w", encoding="utf-8") as f:
            json.dump(entries, f)

    def _resolve_via_subprocess(self, ref, env):
        return subprocess.run(
            [sys.executable, "-c",
             "import sys\n"
             "from vidushi_oa.mail.secrets import SecretResolver\n"
             "print(SecretResolver().resolve(sys.argv[1]))\n",
             ref],
            capture_output=True, text=True, env=env,
        )


class DoctorSecretBackendLineTest(_DoctorWizardTestBase):
    """§S4 AC: doctor's secret-backend line reports `keyring` when a provider is
    genuinely reachable, and `file` with a confirmed marker + an OS-specific fix hint
    when none is — never mentioning the removed vault backends."""

    def test_doctor_reports_keyring_when_reachable_and_confirmed_file_with_os_hint_when_not(self):
        from vidushi_oa import toon as oa_toon

        # Scenario A: a REAL, working, hermetic keyring is reachable.
        keyring_env = {
            "PYTHON_KEYRING_BACKEND": ALT_FILE_KEYRING,
            "XDG_DATA_HOME": self.keyring_home,
        }
        r_ok = self._run(["doctor"], fmt="toon", env_overrides=keyring_env)
        payload_ok = oa_toon.from_toon(r_ok.stdout)
        self.assertEqual(payload_ok.get("secret_backend"), "keyring",
                          f"a reachable keyring provider must report "
                          f"secret_backend: keyring; got {payload_ok!r}")
        combined_ok = (r_ok.stdout + r_ok.stderr).lower()
        self.assertNotIn("1password", combined_ok)
        self.assertNotIn("bitwarden", combined_ok)

        # Scenario B: NO Secret-Service provider is actually reachable (faked KDE
        # desktop; the keyring module is importable but its selected backend
        # unconditionally raises NoKeyringError on set/get). Doctor must fall through
        # to the FILE backend, mark it CONFIRMED (an explicit, stated choice — never a
        # silent downgrade per DN Decision 8), and name the OS-specific remedy.
        no_provider_env = {
            "XDG_CURRENT_DESKTOP": "KDE",
            "PYTHON_KEYRING_BACKEND": FAIL_KEYRING,
        }
        r_gap = self._run(["doctor"], fmt="toon", env_overrides=no_provider_env)
        payload_gap = oa_toon.from_toon(r_gap.stdout)

        self.assertEqual(payload_gap.get("secret_backend"), "file",
                          f"an unreachable Secret-Service provider must report "
                          f"secret_backend: file, not the merely-module-importable "
                          f"'keyring' string; got {payload_gap!r}")
        self.assertIs(payload_gap.get("secret_backend_confirmed"), True,
                       f"reaching the file backend must carry an explicit "
                       f"confirmed/stated-choice marker; got {payload_gap!r}")
        hint = payload_gap.get("secret_backend_hint") or ""
        self.assertIn("KWallet", hint,
                       f"the fix hint must name the OS-specific remedy (KWallet on a "
                       f"faked KDE desktop); got {payload_gap!r}")

        combined_gap = (r_gap.stdout + r_gap.stderr).lower()
        self.assertNotIn("1password", combined_gap)
        self.assertNotIn("bitwarden", combined_gap)


class DoctorRemediationPlanTest(_DoctorWizardTestBase):
    """§S4 AC: an unwired Secret Service AND an unresolvable provider account produce
    an ORDERED remediation plan, each step carrying a boolean human_input flag, with
    the two required human-input steps surfaced in the AXI next[] chain."""

    def test_remediation_plan_orders_secret_service_before_mail_auth_with_human_input_flags(self):
        from vidushi_oa import toon as oa_toon

        broken_ref = "vidushi-oa/gmail:broken@x.com"
        self._seed_accounts([
            {"name": "gmail:broken@x.com", "provider": "gmail",
             "address": "broken@x.com", "secret_ref": broken_ref},
        ])
        env_overrides = {
            "XDG_CURRENT_DESKTOP": "KDE",
            "PYTHON_KEYRING_BACKEND": FAIL_KEYRING,
        }
        r = self._run(["doctor"], fmt="toon", env_overrides=env_overrides)
        payload = oa_toon.from_toon(r.stdout)

        remediation = payload.get("remediation")
        self.assertIsInstance(remediation, list,
                               f"doctor must emit an ordered remediation plan for a "
                               f"detected gap; got {payload!r}")
        self.assertGreaterEqual(len(remediation), 2,
                                 f"remediation plan must have >=2 ordered steps for "
                                 f"this gap (unwired Secret Service + one unresolved "
                                 f"account); got {remediation!r}")
        for step in remediation:
            self.assertIsInstance(step, dict, f"each remediation step must be an "
                                              f"object; got {step!r}")
            self.assertIn("human_input", step,
                          f"every remediation step must carry a human_input field; "
                          f"got {step!r}")
            self.assertIsInstance(step["human_input"], bool,
                                   f"human_input must be a boolean; got {step!r}")

        steps_text = [str(s.get("step", "")) for s in remediation]
        secret_service_idx = next(
            (i for i, t in enumerate(steps_text) if "Secret Service" in t), None)
        mail_auth_idx = next(
            (i for i, t in enumerate(steps_text)
             if "mail-auth" in t and "broken@x.com" in t), None)

        self.assertIsNotNone(
            secret_service_idx,
            f"an 'enable Secret Service' remediation step must be present; "
            f"got {steps_text!r}")
        self.assertIsNotNone(
            mail_auth_idx,
            f"a 'run mail-auth for gmail:broken@x.com' remediation step must be "
            f"present; got {steps_text!r}")
        self.assertLess(secret_service_idx, mail_auth_idx,
                         "the Secret Service step must be ORDERED before the "
                         "mail-auth step (fix the backend before re-authing accounts)")

        self.assertIs(remediation[secret_service_idx]["human_input"], True,
                       "enabling the Secret Service requires human input")
        self.assertIs(remediation[mail_auth_idx]["human_input"], True,
                       "running interactive mail-auth for an account requires "
                       "human input")

        next_list = payload.get("next")
        self.assertIsInstance(next_list, list,
                               f"doctor must surface the remediation as an AXI "
                               f"next[] recommendation chain; got {payload!r}")
        self.assertTrue(any("Secret Service" in n for n in next_list),
                         f"next[] must include the Secret Service remediation; "
                         f"got {next_list!r}")
        self.assertTrue(any("mail-auth" in n and "broken@x.com" in n for n in next_list),
                         f"next[] must include the mail-auth remediation for the "
                         f"broken account; got {next_list!r}")


class DoctorFixWizardTest(_DoctorWizardTestBase):
    """§S4 AC: `voa doctor --fix` instantiates the interactive mail-auth step for a
    gap account rather than merely printing the raw invocation, and never accepts the
    secret via a bare CLI arg (only via mail-auth's own hidden-input/stdin path)."""

    def test_doctor_fix_help_offers_no_secret_or_password_argument(self):
        r = subprocess.run([sys.executable, STORE, "doctor", "--help"],
                            capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--fix", r.stdout,
                       f"doctor --help must list --fix; got:\n{r.stdout}")
        self.assertNotIn("--secret ", r.stdout,
                          f"doctor --help must not offer a bare --secret flag -- the "
                          f"secret may only be entered via mail-auth's hidden-input/"
                          f"stdin path; got:\n{r.stdout}")
        self.assertNotIn("--password", r.stdout,
                          f"doctor --help must not offer a --password flag; "
                          f"got:\n{r.stdout}")

    def test_doctor_fix_chains_into_mail_auth_and_resolves_the_broken_account(self):
        broken_ref = "vidushi-oa/gmail:broken@x.com"
        self._seed_accounts([
            {"name": "gmail:broken@x.com", "provider": "gmail",
             "address": "broken@x.com", "secret_ref": broken_ref},
        ])
        env_overrides = {"VIDUSHI_SECRET_BACKEND": "file"}
        r = self._run(["doctor", "--fix"], fmt="toon", env_overrides=env_overrides,
                       input_=SENTINEL + "\n")

        self.assertEqual(r.returncode, 0,
                          f"doctor --fix must instantiate the interactive mail-auth "
                          f"step and succeed once it is driven via stdin; "
                          f"stdout={r.stdout!r} stderr={r.stderr!r}")
        self.assertNotIn("Traceback", r.stdout)
        self.assertNotIn("Traceback", r.stderr)
        self.assertNotIn(SENTINEL, r.stdout)
        self.assertNotIn(SENTINEL, r.stderr)
        with open(self.accounts_path, encoding="utf-8") as f:
            raw_accounts = f.read()
        self.assertNotIn(SENTINEL, raw_accounts)

        # Prove --fix actually DROVE the interactive mail-auth flow and stored the
        # secret under the account's real secret_ref -- not merely printed guidance.
        proof_env = dict(self.env)
        proof_env.update(env_overrides)
        proof = self._resolve_via_subprocess(broken_ref, proof_env)
        self.assertEqual(proof.returncode, 0, proof.stderr)
        self.assertEqual(proof.stdout.strip(), SENTINEL,
                          "doctor --fix must have driven the interactive mail-auth "
                          "flow and stored the secret under the account's "
                          "secret_ref -- not merely printed the raw invocation")


class DoctorFixWiredFromNonTestCallerTest(unittest.TestCase):
    """§S4 AC (caller-existence): `--fix` is registered on the doctor subparser AND
    actually consumed via `a.fix` in vidushi_oa/_cli.py -- not merely declared and
    ignored. Checks the literal flag spelling and the dotted-attribute read
    separately, rather than a bare substring count an unrelated variable could
    already satisfy."""

    def test_fix_flag_is_both_registered_and_consumed_in_cli_source(self):
        with open(CLI_SRC, encoding="utf-8") as f:
            src = f.read()
        self.assertGreaterEqual(
            src.count("--fix"), 1,
            f"--fix must be registered as an argparse argument on the doctor "
            f"subparser in vidushi_oa/_cli.py (found {src.count('--fix')} "
            f"reference(s))")
        self.assertGreaterEqual(
            src.count("a.fix"), 1,
            f"the doctor command handler must read a.fix to drive the --fix "
            f"remediation-wizard path (found {src.count('a.fix')} reference(s)) -- "
            f"a flag that is declared but never consumed is not wired")


class DoctorFixPreservesSendAndAliasesTest(_DoctorWizardTestBase):
    """CR-OA-022 regression: `doctor --fix` re-authing a send-capable account with
    aliases must PRESERVE both `send` and `aliases`. The re-auth path previously
    called `_provision_account_secret(provider, address, auth_mode)` only, so
    `add_account` reset `send`->False and `aliases`->[] — silently stripping the
    account's send capability and configured From identities on every re-auth."""

    def test_doctor_fix_preserves_send_and_aliases_after_reauth(self):
        broken_ref = "vidushi-oa/fastmail:sender@x.com"
        aliases = ["alias1@x.com", "masked-2@fastmailmail.com"]
        self._seed_accounts([
            {"name": "fastmail:sender@x.com", "provider": "fastmail",
             "address": "sender@x.com", "secret_ref": broken_ref,
             "auth_mode": "password", "send": True, "aliases": aliases},
        ])
        env_overrides = {"VIDUSHI_SECRET_BACKEND": "file"}
        r = self._run(["doctor", "--fix"], fmt="toon", env_overrides=env_overrides,
                      input_=SENTINEL + "\n")

        self.assertEqual(r.returncode, 0,
                         f"doctor --fix must succeed once driven via stdin; "
                         f"stdout={r.stdout!r} stderr={r.stderr!r}")
        self.assertNotIn("Traceback", r.stdout + r.stderr)

        with open(self.accounts_path, encoding="utf-8") as f:
            entries = json.load(f)
        entry = next(e for e in entries if e.get("name") == "fastmail:sender@x.com")
        self.assertIs(entry.get("send"), True,
                      f"doctor --fix must KEEP the account's send capability after "
                      f"re-auth, not reset it to False; got {entry!r}")
        self.assertEqual(entry.get("aliases"), aliases,
                         f"doctor --fix must KEEP the account's configured aliases "
                         f"after re-auth, not drop them; got {entry!r}")

        # The re-auth genuinely re-stored the secret under the account's real ref.
        proof_env = dict(self.env)
        proof_env.update(env_overrides)
        proof = self._resolve_via_subprocess(broken_ref, proof_env)
        self.assertEqual(proof.returncode, 0, proof.stderr)
        self.assertEqual(proof.stdout.strip(), SENTINEL)


if __name__ == "__main__":
    unittest.main()
