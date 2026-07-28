"""CR-OA-023 §S3 — OS-aware secret provisioning in `voa setup` + pre-flight (RED).

Per docs/changes/CR-OA-023-keyring-primary-os-aware-secret-setup.md §S3 / ACs, `voa setup`
gains a **`--secret-backend {auto,keyring,file}`** option (default ``auto``) plus OS-aware
secret provisioning:

  - **auto**: detect the host OS + Secret-Service provider. If a provider is reachable,
    select keyring and pre-flight it. If NO provider is reachable (e.g. a faked KDE desktop
    with no Secret Service running), emit guidance naming **KWallet / `org.freedesktop.secrets`**
    (KDE) or gnome-keyring/libsecret (GNOME/other freedesktop), and must NOT silently fall
    through to writing a file-backend secret — it exits/reports the gap instead.
  - **file**: the explicit, CONFIRMED file-backend choice — provisions the file backend and
    records a structured status with `secret_backend: file` plus a `confirmed`/stated-choice
    marker (never reached silently).
  - **keyring**: force keyring; pre-flight must pass or the failure is reported structurally.
  - **Pre-flight**: on the selected backend, a `set`->`get` round-trip on a throwaway ref is
    performed and its success/failure is reported in the structured status.
  - **No personal data in the client**: no real personal mailbox address/alias/domain
    anywhere in `vidushi_oa/` source; the wizard/mail-auth field prompts show a fictitious
    `example`-style sample, never a pre-filled real value.

NONE of this exists yet today:
  - `setup` has no `--secret-backend` option at all (`su = add_parser("setup");
    su.add_argument("--check", ...)` only) -> passing `--secret-backend ...` is an argparse
    error (exit 2, "unrecognized arguments").
  - There is no OS/desktop detection, no Secret-Service pre-flight, and no confirmed-choice
    marker anywhere in `vidushi_oa/_cli.py`.
  - `mail-auth --address`/`--provider` carry no help text at all, so no `you@fastmail.com` /
    `example.`-style placeholder is rendered.

Hermetic-environment choices (NO real OS Secret Service, NO live network):
  - Desktop is faked via `XDG_CURRENT_DESKTOP` (KDE / GNOME).
  - "Provider absent" is faked deterministically via
    `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring` — a real backend shipped with the
    `keyring` package itself (verified installed: keyring 25.7.0) whose `get_password` /
    `set_password` (aliased to the same method) unconditionally raise
    `keyring.errors.NoKeyringError` -- simulating "no Secret Service reachable" without
    touching any real D-Bus session.
  - "Provider present / working" is faked via
    `PYTHON_KEYRING_BACKEND=keyrings.alt.file.PlaintextKeyring` (the `[test]` extra's
    file-backed keyring, verified installed: keyrings.alt 5.0.2), relocated under a fresh
    `XDG_DATA_HOME` tempdir so it never touches a real OS keyring -- the same pattern already
    used by `test_cr_oa_020_mail_auth_doctor.py`.
  - `VIDUSHI_BACKEND=sqlite` + a tmp `VIDUSHI_SQLITE_PATH` isolate the store side of `setup`.
  - `VIDUSHI_SECRETS_FILE` points the file backend at a tmp path so "no secrets file was
    created" is directly observable.
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

FAIL_KEYRING = "keyring.backends.fail.Keyring"
# A REAL, storable, file-backed keyring from the `keyrings.alt` package (the `[test]`
# extra) -- see test_cr_oa_020_mail_auth_doctor.py for why this (and not the null backend)
# is used to prove a round-trip actually stores/retrieves something.
ALT_FILE_KEYRING = "keyrings.alt.file.PlaintextKeyring"

# Real personal identifiers that must never appear in the shipped client source (the
# provider infra hostnames like imap.gmail.com are explicitly exempt per the AC).
_REAL_PERSONAL_MARKERS = ("antojk", "anthilllabs", "new.book1604")


class _SetupSecretBackendTestBase(unittest.TestCase):
    """Env isolation for OS/desktop + keyring-provider faking, and a tmp store +
    secrets file so no real OS keyring / Secret Service / on-disk state leaks in or out."""

    ENV_KEYS = (
        "XDG_CURRENT_DESKTOP",
        "PYTHON_KEYRING_BACKEND",
        "XDG_DATA_HOME",
        "VIDUSHI_SECRET_BACKEND",
        "VIDUSHI_SECRETS_FILE",
        "VIDUSHI_BACKEND",
        "VIDUSHI_SQLITE_PATH",
        "VIDUSHI_FORMAT",
    )

    def setUp(self):
        self._saved_env = {k: os.environ.get(k) for k in self.ENV_KEYS}
        for k in self.ENV_KEYS:
            os.environ.pop(k, None)

        self.tmp = tempfile.mkdtemp(prefix="oa-cr023-s3-")
        self.secrets_path = os.path.join(self.tmp, "secrets.json")
        self.sqlite_path = os.path.join(self.tmp, "oa.db")
        self.keyring_home = os.path.join(self.tmp, "keyring-home")
        os.makedirs(self.keyring_home, exist_ok=True)

        self.env = dict(os.environ)
        self.env["VIDUSHI_BACKEND"] = "sqlite"
        self.env["VIDUSHI_SQLITE_PATH"] = self.sqlite_path
        self.env["VIDUSHI_SECRETS_FILE"] = self.secrets_path

    def tearDown(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, args, fmt=None, env_overrides=None):
        env = dict(self.env)
        if fmt:
            env["VIDUSHI_FORMAT"] = fmt
        if env_overrides:
            env.update(env_overrides)
        return subprocess.run(
            [sys.executable, STORE, *args], capture_output=True, text=True, env=env,
        )


class SetupHelpExposesSecretBackendOptionTest(_SetupSecretBackendTestBase):
    """§S3 AC (caller-existence): `voa setup --help` shows the new option."""

    def test_setup_help_lists_secret_backend_flag_with_three_choices(self):
        r = subprocess.run([sys.executable, STORE, "setup", "--help"],
                            capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--secret-backend", r.stdout,
                       f"setup --help must list --secret-backend; got:\n{r.stdout}")
        for choice in ("auto", "keyring", "file"):
            self.assertIn(choice, r.stdout,
                           f"setup --help must show the {choice!r} choice; got:\n{r.stdout}")


class SetupAutoNoProviderGuidanceTest(_SetupSecretBackendTestBase):
    """§S3 AC: with the Secret-Service provider absent, `auto` names the OS-specific
    remedy and never silently provisions the file backend instead."""

    def test_auto_with_faked_kde_and_no_provider_names_kwallet_and_secret_service(self):
        r = self._run(["setup", "--secret-backend", "auto"],
                       env_overrides={"XDG_CURRENT_DESKTOP": "KDE",
                                      "PYTHON_KEYRING_BACKEND": FAIL_KEYRING})
        combined = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0,
                             f"auto with no reachable provider must report a gap, not "
                             f"succeed silently; stdout={r.stdout!r} stderr={r.stderr!r}")
        self.assertIn("KWallet", combined,
                       f"KDE guidance must name KWallet; got:\n{combined}")
        self.assertIn("org.freedesktop.secrets", combined,
                       f"KDE guidance must name org.freedesktop.secrets; got:\n{combined}")
        self.assertFalse(
            os.path.exists(self.secrets_path),
            "auto must NOT silently write a file-backend secret when no provider "
            "is reachable",
        )

    def test_auto_with_faked_gnome_and_no_provider_names_gnome_keyring_or_libsecret(self):
        r = self._run(["setup", "--secret-backend", "auto"],
                       env_overrides={"XDG_CURRENT_DESKTOP": "GNOME",
                                      "PYTHON_KEYRING_BACKEND": FAIL_KEYRING})
        combined = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0,
                             f"auto with no reachable provider must report a gap, not "
                             f"succeed silently; stdout={r.stdout!r} stderr={r.stderr!r}")
        self.assertTrue(
            "gnome-keyring" in combined.lower() or "libsecret" in combined.lower(),
            f"GNOME guidance must name gnome-keyring or libsecret; got:\n{combined}",
        )
        self.assertFalse(
            os.path.exists(self.secrets_path),
            "auto must NOT silently write a file-backend secret when no provider "
            "is reachable",
        )

    def test_auto_headless_offers_but_never_silently_takes_the_file_backend_choice(self):
        # No XDG_CURRENT_DESKTOP at all (headless) + no reachable provider: per §S3
        # ("headless / no provider -> the 0600 file, offered as a stated, confirmed
        # user choice -- never a silent downgrade"), `auto` must NAME the explicit
        # `--secret-backend file` escape hatch as guidance rather than taking it itself.
        # Asserting `"unrecognized arguments"` is ABSENT is what stops this test from
        # vacuously passing today merely because the flag doesn't parse yet (today's
        # argparse rejection also happens to leave no secrets file behind and exit
        # non-zero, which would otherwise make this pass for the wrong reason).
        r = self._run(["setup", "--secret-backend", "auto"], fmt="json",
                       env_overrides={"PYTHON_KEYRING_BACKEND": FAIL_KEYRING})
        combined = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0,
                             f"headless auto with no provider must report a gap; "
                             f"stdout={r.stdout!r} stderr={r.stderr!r}")
        self.assertNotIn("unrecognized arguments", combined,
                          f"the gap must be a deliberate, reported outcome of the "
                          f"OS-aware path -- not merely an unimplemented CLI flag; "
                          f"got:\n{combined}")
        self.assertIn("--secret-backend file", combined,
                       f"headless guidance must name the explicit file-backend escape "
                       f"hatch as the stated choice the user must confirm; got:\n{combined}")
        self.assertNotIn('"secret_backend":"file"', r.stdout.replace(" ", ""),
                          "auto must never silently downgrade to the file backend without "
                          "an explicit confirmed choice")
        self.assertFalse(
            os.path.exists(self.secrets_path),
            "auto must NOT silently write a file-backend secret in a headless/no-provider "
            "environment",
        )


class SetupExplicitFileChoiceTest(_SetupSecretBackendTestBase):
    """§S3 AC: the file backend is chosen ONLY via the explicit `--secret-backend file`
    step, and the structured status records `secret_backend: file` as a stated,
    confirmed choice."""

    def test_explicit_file_secret_backend_is_recorded_as_confirmed_stated_choice(self):
        r = self._run(["setup", "--secret-backend", "file"],
                       env_overrides={"PYTHON_KEYRING_BACKEND": FAIL_KEYRING})
        self.assertEqual(r.returncode, 0,
                          f"an explicit, confirmed file-backend choice must succeed; "
                          f"stdout={r.stdout!r} stderr={r.stderr!r}")

        from vidushi_oa import toon as oa_toon
        payload = oa_toon.from_toon(r.stdout)
        self.assertEqual(payload.get("secret_backend"), "file",
                          f"structured status must record secret_backend: file; got {payload!r}")
        self.assertIs(payload.get("confirmed"), True,
                       f"the file backend must carry an explicit confirmed/stated-choice "
                       f"marker; got {payload!r}")


class SetupPreflightRoundTripTest(_SetupSecretBackendTestBase):
    """§S3 AC: the selected backend undergoes a `set`->`get` round-trip and the
    structured status reports its success/failure."""

    def test_preflight_round_trip_reports_success_for_a_working_keyring_backend(self):
        r = self._run(
            ["setup", "--secret-backend", "keyring"], fmt="json",
            env_overrides={"PYTHON_KEYRING_BACKEND": ALT_FILE_KEYRING,
                           "XDG_DATA_HOME": self.keyring_home},
        )
        self.assertEqual(r.returncode, 0,
                          f"a working keyring backend must pre-flight cleanly; "
                          f"stdout={r.stdout!r} stderr={r.stderr!r}")
        payload = json.loads(r.stdout.strip())
        self.assertEqual(payload.get("secret_backend"), "keyring",
                          f"forced keyring must be reported as selected; got {payload!r}")
        preflight = payload.get("preflight")
        self.assertIsInstance(preflight, dict,
                               f"structured status must carry a preflight report; got {payload!r}")
        self.assertIs(preflight.get("ok"), True,
                       f"a successful set->get round-trip must report ok: true; got {preflight!r}")

    def test_preflight_round_trip_reports_failure_for_a_broken_keyring_backend(self):
        r = self._run(
            ["setup", "--secret-backend", "keyring"], fmt="json",
            env_overrides={"PYTHON_KEYRING_BACKEND": FAIL_KEYRING},
        )
        self.assertNotEqual(r.returncode, 0,
                             f"a broken keyring backend must fail the pre-flight; "
                             f"stdout={r.stdout!r} stderr={r.stderr!r}")
        self.assertNotIn("Traceback", r.stdout)
        self.assertNotIn("Traceback", r.stderr)
        payload = json.loads(r.stdout.strip())
        self.assertEqual(payload.get("secret_backend"), "keyring")
        preflight = payload.get("preflight")
        self.assertIsInstance(preflight, dict,
                               f"structured status must carry a preflight report; got {payload!r}")
        self.assertIs(preflight.get("ok"), False,
                       f"a failing set->get round-trip must report ok: false; got {preflight!r}")


class NoPersonalDataInClientTest(unittest.TestCase):
    """§S3 AC: no real personal mailbox address/alias/domain anywhere in `vidushi_oa/`
    source, and the wizard/mail-auth field prompts render a fictitious `example`-style
    sample instead of a pre-filled real value.

    Folded into ONE test (rather than a standalone always-green source grep) because the
    grep half alone already holds true today with nothing implemented -- the meaningful,
    currently-FAILING half is the second: today `--address`/`--provider` on `mail-auth`
    carry no help text at all, so no example-style placeholder is rendered anywhere."""

    def test_no_real_personal_address_in_source_and_field_prompts_use_fictitious_sample(self):
        package_dir = os.path.join(ROOT, "vidushi_oa")
        offending = []
        for dirpath, _dirnames, filenames in os.walk(package_dir):
            for filename in filenames:
                if not filename.endswith(".py"):
                    continue
                path = os.path.join(dirpath, filename)
                with open(path, "r", encoding="utf-8") as fh:
                    content = fh.read()
                for marker in _REAL_PERSONAL_MARKERS:
                    if marker in content:
                        offending.append((os.path.relpath(path, ROOT), marker))
        self.assertEqual(offending, [],
                          f"real personal identifiers must never appear in vidushi_oa/ "
                          f"source: {offending}")

        env = dict(os.environ)
        env.pop("VIDUSHI_FORMAT", None)
        r = subprocess.run([sys.executable, STORE, "mail-auth", "--help"],
                            capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(
            "you@fastmail.com" in r.stdout or "example." in r.stdout,
            f"mail-auth --help must render a fictitious example-style address sample "
            f"(e.g. you@fastmail.com or an example.-domain placeholder), not a "
            f"pre-filled real value; got:\n{r.stdout}",
        )
        for marker in _REAL_PERSONAL_MARKERS:
            self.assertNotIn(marker, r.stdout,
                              f"mail-auth --help must never leak a real personal "
                              f"identifier; got:\n{r.stdout}")


class SetupSecretBackendWiredFromNonTestCallerTest(unittest.TestCase):
    """§S3 AC (caller-existence): the `--secret-backend` option is both defined AND
    actually consumed by a non-test caller in `vidushi_oa/_cli.py` -- not merely
    declared and ignored. NOTE: `cmd_doctor` already has an unrelated local variable
    named `secret_backend` (CR-OA-020 §S7), so this checks the literal CLI flag
    spelling `--secret-backend` (argparse registration) AND the dotted-attribute read
    `a.secret_backend` (the command handler consuming it) separately, rather than a
    bare substring count that unrelated code would already satisfy."""

    def test_secret_backend_flag_is_both_registered_and_consumed_in_cli_source(self):
        with open(CLI_SRC, encoding="utf-8") as f:
            src = f.read()
        self.assertGreaterEqual(
            src.count("--secret-backend"), 1,
            f"--secret-backend must be registered as an argparse argument in "
            f"vidushi_oa/_cli.py (found {src.count('--secret-backend')} reference(s))",
        )
        self.assertGreaterEqual(
            src.count("a.secret_backend"), 1,
            f"the command handler must read a.secret_backend to act on the OS-aware "
            f"provisioning + pre-flight path (found {src.count('a.secret_backend')} "
            f"reference(s)) -- a flag that is declared but never consumed is not wired",
        )


if __name__ == "__main__":
    unittest.main()
