"""CR-OA-020 §S7 — interactive `voa mail-auth` (hidden/stdin secret) + `voa doctor` (RED).

Covers spec lines 62-73 / AC lines 101-104:

  - `voa mail-auth` (extended): when `--secret-ref` is NOT given, obtains the secret via a
    hidden prompt (interactive) or ONE line of stdin (non-interactive/CI escape — what every
    test below drives, since a subprocess test has no real tty), then stores it through a
    `SecretResolver` under a DERIVED reference `vidushi-oa/{provider}:{address}` and persists
    that reference (never the secret) via `vidushi_oa.mail.accounts.add_account`. The secret
    must NEVER be accepted as a CLI arg (no `--secret`/`--password` flag).
  - `voa doctor` (new verb): a full AXI/TOON read absorbing `setup --check` — reports the
    engine version, the active STORE backend (name + `check()` reachability), the active
    SECRET backend name, and one row per configured account:
    `{account, provider, kind, resolves, hint}` (`hint` only meaningful/non-empty when
    `resolves` is False). Exits non-zero when any checked item (store unreachable, OR any
    account's reference fails to resolve) fails. Never prints a secret value.

Neither the stdin-secret path on `mail-auth` nor the `doctor` verb exist yet:
  - `mail-auth` today REQUIRES `--secret-ref` (argparse `required=True`) and has no stdin/
    getpass path at all, so a call without `--secret-ref` fails at argparse (exit 2) before
    `cmd_mail_auth` ever runs.
  - `doctor` is not a registered subcommand, so `store.py doctor ...` fails at argparse
    ("invalid choice") — RED.

Hermetic-environment choices (NO live creds / NO real OS keyring):
  - Every subprocess run points `VIDUSHI_MAIL_CONFIG` at a tmp accounts file and
    `VIDUSHI_SECRETS_FILE` at a tmp secrets file, keeping every credential reference and
    secret inside our own tmpdir regardless of the host running this suite.
  - For the stdin/e2e + doctor tests we pin `VIDUSHI_SECRET_BACKEND=file` so the secret sink
    is the tmp FILE backend (a plain 0600 JSON file under our own tmpdir) — NEVER the real
    keyring.
  - For the ONE test that must exercise the "auto-selected keyring primary stores the secret
    and warns" path (AC-b), we additionally force
    `PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring`
    — a backend that ships with `keyring` itself (no extra dependency), whose `set_password`/
    `get_password` are no-ops that never touch real OS secret storage and never raise. This
    was verified necessary: probing this very sandbox shows `keyring.get_keyring()` resolves
    to `keyring.backends.fail.Keyring` by default (no D-Bus/SecretService session available),
    and that backend's `set_password` RAISES `keyring.errors.NoKeyringError` rather than
    quietly no-op'ing — exercising the real default backend here would either hit a real
    secret store (on a host that has one) or crash on a `NoKeyringError` that has nothing to
    do with the behaviour §S7 is trying to pin. The null backend isolates the test to exactly
    the "did mail-auth pick keyring and warn, without crashing" question.
  - `VIDUSHI_BACKEND=sqlite` + a tmp `VIDUSHI_SQLITE_PATH` isolate the store `doctor` probes.

Sentinel-leak checks grep the ACCOUNTS file + stdout + stderr of the CLI invocation under
test for the sentinel secret (never the tmp secrets FILE backend itself, which legitimately
holds it by design).
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

SENTINEL = "S3NTINEL-SECRET"
NULL_KEYRING = "keyring.backends.null.Keyring"
# A REAL, storable, file-backed keyring from the `keyrings.alt` package (the `[test]` extra),
# used for the ONE test that must prove the "falls back to keyring" path actually stores the
# secret. `keyrings.alt` depends on `keyring`, so installing it guarantees `keyring` itself is
# importable regardless of whether this project's own optional `mail` extra is installed —
# unlike `keyring.backends.null.Keyring`, whose apparent "primary lands on keyring" behaviour
# depends on that installation detail and whose set/get are no-ops that never really store
# anything. Its file store is relocated under `XDG_DATA_HOME` to a fresh per-test tempdir so
# it is fully hermetic and never touches the real OS keyring.
ALT_FILE_KEYRING = "keyrings.alt.file.PlaintextKeyring"


class MailAuthDoctorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="oa-cr020-s7-")
        self.accounts_path = os.path.join(self.tmp, "accounts.json")
        self.secrets_path = os.path.join(self.tmp, "secrets.json")
        self.sqlite_path = os.path.join(self.tmp, "oa.db")

        self.env = dict(os.environ)
        self.env["VIDUSHI_MAIL_CONFIG"] = self.accounts_path
        self.env["VIDUSHI_SECRETS_FILE"] = self.secrets_path
        self.env["VIDUSHI_SECRET_BACKEND"] = "file"
        self.env["VIDUSHI_BACKEND"] = "sqlite"
        self.env["VIDUSHI_SQLITE_PATH"] = self.sqlite_path
        self.env.pop("PYTHON_KEYRING_BACKEND", None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, args, fmt="toon", input_=None, env_overrides=None):
        env = dict(self.env)
        env["VIDUSHI_FORMAT"] = fmt
        if env_overrides:
            env.update(env_overrides)
        return subprocess.run(
            [sys.executable, STORE, *args], capture_output=True, text=True, env=env, input=input_,
        )

    def _seed_accounts(self, entries):
        with open(self.accounts_path, "w", encoding="utf-8") as f:
            json.dump(entries, f)

    def _seed_secret(self, ref, value):
        with open(self.secrets_path, "w", encoding="utf-8") as f:
            json.dump({ref: value}, f)

    # ------------------------------------------------------------------
    # AC-a — mail-auth non-interactive stdin persists a reference only
    # ------------------------------------------------------------------

    def test_mail_auth_stdin_mode_persists_reference_only_and_leaks_no_secret(self):
        # The secret must never be acceptable as a CLI arg in the first place — no bare
        # `--secret`/`--password` flag (folded in here rather than as its own always-green
        # test, since it holds true even before GREEN and the point of this test is the
        # NEW stdin-secret behaviour below, which does not work yet).
        help_r = subprocess.run([sys.executable, STORE, "mail-auth", "--help"],
                                 capture_output=True, text=True, env=self.env)
        self.assertEqual(help_r.returncode, 0, help_r.stderr)
        self.assertNotIn("--secret ", help_r.stdout,
                          f"mail-auth --help must not offer a bare --secret flag; got:\n{help_r.stdout}")
        self.assertNotIn("--password", help_r.stdout,
                          f"mail-auth --help must not offer a --password flag; got:\n{help_r.stdout}")

        r = self._run(["mail-auth", "--provider", "gmail", "--address", "me@x.com"],
                       input_=SENTINEL + "\n")
        self.assertEqual(r.returncode, 0, f"stdin mail-auth must exit 0; stderr={r.stderr!r}")
        self.assertTrue(r.stdout.strip(), "mail-auth must emit an AXI status object")

        with open(self.accounts_path, encoding="utf-8") as f:
            raw_accounts = f.read()
        accounts = json.loads(raw_accounts)
        self.assertEqual(len(accounts), 1)
        entry = accounts[0]
        self.assertEqual(set(entry.keys()),
                         {"name", "provider", "address", "secret_ref", "auth_mode"})
        self.assertEqual(entry["provider"], "gmail")
        self.assertEqual(entry["address"], "me@x.com")
        self.assertEqual(entry["auth_mode"], "password",
                         "default mail-auth must persist auth_mode 'password'")
        derived_ref = entry["secret_ref"]
        self.assertEqual(derived_ref, "vidushi-oa/gmail:me@x.com",
                          "the derived secret_ref must follow vidushi-oa/{provider}:{address}")

        # Grep every user-facing artifact for the sentinel -> 0 matches. (The tmp secrets
        # FILE backend legitimately holds it by design and is deliberately NOT checked here.)
        self.assertNotIn(SENTINEL, raw_accounts)
        self.assertNotIn(SENTINEL, r.stdout)
        self.assertNotIn(SENTINEL, r.stderr)

        # Prove store+reference wiring: the derived ref actually resolves back to the
        # sentinel through the SAME SecretResolver machinery mail-auth used to store it.
        # (This side-channel proof process is not one of the leak-checked artifacts above.)
        proof = subprocess.run(
            [sys.executable, "-c",
             "import sys\n"
             "from vidushi_oa.mail.secrets import SecretResolver\n"
             "print(SecretResolver().resolve(sys.argv[1]))\n",
             derived_ref],
            capture_output=True, text=True, env=self.env,
        )
        self.assertEqual(proof.returncode, 0, proof.stderr)
        self.assertEqual(proof.stdout.strip(), SENTINEL,
                          "the derived secret_ref must resolve back to the stored secret")

    def test_mail_auth_stdin_mode_json_variant_is_clean_json_status(self):
        r = self._run(["mail-auth", "--provider", "yahoo", "--address", "me2@x.com"],
                       fmt="json", input_=SENTINEL + "\n")
        self.assertEqual(r.returncode, 0, f"stdin mail-auth --json must exit 0; stderr={r.stderr!r}")
        payload = json.loads(r.stdout.strip())
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload.get("provider"), "yahoo")
        self.assertEqual(payload.get("address"), "me2@x.com")
        self.assertNotIn(SENTINEL, r.stdout)
        self.assertNotIn(SENTINEL, r.stderr)

    def test_mail_auth_xoauth2_persists_auth_mode_and_reference_without_leaking_secret(self):
        # Gmail XOAUTH2: the entered secret is a JSON credential blob; only a derived
        # reference + auth_mode="xoauth2" must land in the registry, never the blob.
        blob = json.dumps({"client_id": "cid", "client_secret": SENTINEL,
                           "refresh_token": "rtok"})
        r = self._run(["mail-auth", "--provider", "gmail", "--address", "ws@x.com",
                       "--auth-mode", "xoauth2"], input_=blob + "\n")
        self.assertEqual(r.returncode, 0, f"xoauth2 mail-auth must exit 0; stderr={r.stderr!r}")

        with open(self.accounts_path, encoding="utf-8") as f:
            raw_accounts = f.read()
        accounts = json.loads(raw_accounts)
        self.assertEqual(len(accounts), 1)
        entry = accounts[0]
        self.assertEqual(entry["provider"], "gmail")
        self.assertEqual(entry["auth_mode"], "xoauth2")
        self.assertEqual(entry["secret_ref"], "vidushi-oa/gmail:ws@x.com")

        # The credential blob (and its embedded sentinel) never reaches any user-facing
        # artifact — only the tmp file backend legitimately holds it.
        self.assertNotIn(SENTINEL, raw_accounts)
        self.assertNotIn(SENTINEL, r.stdout)
        self.assertNotIn(SENTINEL, r.stderr)

    def test_mail_auth_rejects_xoauth2_for_non_gmail_provider(self):
        # xoauth2 is honoured only in the gmail factory branch; requesting it for
        # yahoo/fastmail must fail LOUDLY with a structured error + exit 1 BEFORE
        # anything is persisted, rather than silently storing a mis-auth entry.
        blob = json.dumps({"client_id": "cid", "client_secret": SENTINEL,
                           "refresh_token": "rtok"})
        r = self._run(["mail-auth", "--provider", "yahoo", "--address", "y@x.com",
                       "--auth-mode", "xoauth2"], fmt="json", input_=blob + "\n")

        self.assertNotEqual(r.returncode, 0,
                            "xoauth2 with a non-gmail provider must exit non-zero")
        self.assertNotIn("Traceback", r.stdout)
        self.assertNotIn("Traceback", r.stderr)
        payload = json.loads(r.stdout.strip())
        self.assertIn("error", payload)
        self.assertNotIn(SENTINEL, r.stdout)
        self.assertNotIn(SENTINEL, r.stderr)
        # Nothing was persisted: the accounts registry must not have been created/written.
        self.assertFalse(os.path.exists(self.accounts_path) and
                         json.load(open(self.accounts_path, encoding="utf-8")),
                         "a rejected xoauth2 mail-auth must write no account entry")

    def test_mail_accounts_unresolvable_secret_is_structured_error_not_traceback(self):
        # A configured account whose secret_ref cannot be resolved (file backend, no
        # secret seeded) makes build_client raise LookupError during eager resolution;
        # the mail-* verbs must render that as a structured error + exit 1, no traceback.
        self._seed_accounts([
            {"name": "gmail:x@x.com", "provider": "gmail", "address": "x@x.com",
             "secret_ref": "vidushi-oa/gmail:x@x.com"},
        ])
        env = self._doctor_env()
        env["VIDUSHI_FORMAT"] = "json"
        r = subprocess.run([sys.executable, STORE, "mail-accounts", "--json"],
                           capture_output=True, text=True, env=env)

        self.assertNotEqual(r.returncode, 0,
                            "mail-accounts must exit non-zero when a secret_ref cannot resolve")
        self.assertNotIn("Traceback", r.stdout)
        self.assertNotIn("Traceback", r.stderr)
        payload = json.loads(r.stdout.strip())
        self.assertIn("error", payload)

    # ------------------------------------------------------------------
    # AC-b — keyring fallback + warning, no crash
    # ------------------------------------------------------------------

    def test_mail_auth_stores_in_keyring_with_warning_when_backend_auto_selected(self):
        # Auto-select the primary backend (no VIDUSHI_SECRET_BACKEND override): the resolver
        # must land on the keyring, not crash, and warn. Point it at
        # a REAL, storable, hermetic keyring (keyrings.alt's file-backed PlaintextKeyring,
        # relocated under a fresh XDG_DATA_HOME tempdir) rather than the null no-op backend, so
        # the fallback actually stores the secret and the "keyring" warning is proven true, not
        # just emitted regardless of whether anything landed anywhere.
        keyring_home = tempfile.mkdtemp(prefix="oa-cr020-keyring-")
        self.addCleanup(shutil.rmtree, keyring_home, ignore_errors=True)
        env_overrides = {
            "PYTHON_KEYRING_BACKEND": ALT_FILE_KEYRING,
            "XDG_DATA_HOME": keyring_home,
        }
        env = dict(self.env)
        env.pop("VIDUSHI_SECRET_BACKEND", None)
        env.update(env_overrides)
        env["VIDUSHI_FORMAT"] = "toon"

        r = subprocess.run(
            [sys.executable, STORE, "mail-auth", "--provider", "fastmail", "--address", "me3@x.com"],
            capture_output=True, text=True, env=env, input=SENTINEL + "\n",
        )
        self.assertEqual(r.returncode, 0,
                          f"keyring fallback must not crash; stdout={r.stdout!r} stderr={r.stderr!r}")
        self.assertTrue(r.stderr.strip(), "expected a fallback warning on stderr")
        self.assertIn("keyring", r.stderr.lower(),
                       f"warning must name the keyring fallback destination; stderr={r.stderr!r}")
        self.assertNotIn(SENTINEL, r.stdout)
        self.assertNotIn(SENTINEL, r.stderr)

        # Prove the secret actually landed in the (real, hermetic) keyring rather than the
        # warning being emitted regardless of whether anything was really stored: resolve the
        # derived ref back through the same SecretResolver chain, in the same keyring env.
        # (Deliberately NOT grepped for the sentinel above -- the keyring's own tempdir store
        # legitimately holds it by design, mirroring how the file backend is excluded.)
        proof = subprocess.run(
            [sys.executable, "-c",
             "import sys\n"
             "from vidushi_oa.mail.secrets import SecretResolver\n"
             "print(SecretResolver().resolve(sys.argv[1]))\n",
             "vidushi-oa/fastmail:me3@x.com"],
            capture_output=True, text=True, env=env,
        )
        self.assertEqual(proof.returncode, 0, proof.stderr)
        self.assertEqual(proof.stdout.strip(), SENTINEL,
                          "the secret must be retrievable from the real keyring backend it fell back to")

    # ------------------------------------------------------------------
    # AC-c — doctor: happy path, failing path, --json, exit codes, no secrets
    # ------------------------------------------------------------------

    def _doctor_env(self):
        env = dict(self.env)
        env["VIDUSHI_SECRET_BACKEND"] = "file"
        env["PYTHON_KEYRING_BACKEND"] = NULL_KEYRING
        return env

    def test_doctor_reports_a_broken_account_with_hint_and_exits_nonzero(self):
        good_ref = "vidushi-oa/fastmail:good@x.com"
        bad_ref = "vidushi-oa/gmail:broken@x.com"
        self._seed_accounts([
            {"name": "fastmail:good@x.com", "provider": "fastmail",
             "address": "good@x.com", "secret_ref": good_ref},
            {"name": "gmail:broken@x.com", "provider": "gmail",
             "address": "broken@x.com", "secret_ref": bad_ref},
        ])
        self._seed_secret(good_ref, SENTINEL)

        env = self._doctor_env()
        env["VIDUSHI_FORMAT"] = "toon"
        r = subprocess.run([sys.executable, STORE, "doctor"], capture_output=True, text=True, env=env)

        self.assertNotEqual(r.returncode, 0,
                             "doctor must exit non-zero when a checked account fails to resolve")
        self.assertNotIn(SENTINEL, r.stdout)
        self.assertNotIn(SENTINEL, r.stderr)

        from vidushi_oa import toon as oa_toon
        payload = oa_toon.from_toon(r.stdout)

        from vidushi_oa import __version__
        self.assertIn(__version__, str(payload.get("engine", "")),
                       f"doctor must report the engine version; got {payload!r}")

        store_backend = payload.get("store_backend")
        self.assertIsInstance(store_backend, dict, f"doctor must report store_backend; got {payload!r}")
        self.assertEqual(store_backend.get("name"), "sqlite")
        self.assertIs(store_backend.get("ok"), True)

        self.assertEqual(payload.get("secret_backend"), "file")

        rows = payload.get("accounts")
        self.assertIsInstance(rows, list)
        by_name = {row["account"]: row for row in rows}
        self.assertEqual(set(by_name.keys()),
                          {"fastmail:good@x.com", "gmail:broken@x.com"})

        good_row = by_name["fastmail:good@x.com"]
        self.assertEqual(good_row["provider"], "fastmail")
        self.assertIs(good_row["resolves"], True)
        self.assertFalse(good_row.get("hint"), f"a resolving account must carry no fix hint: {good_row!r}")

        bad_row = by_name["gmail:broken@x.com"]
        self.assertEqual(bad_row["provider"], "gmail")
        self.assertIs(bad_row["resolves"], False)
        self.assertTrue(bad_row.get("hint"), f"a broken account must carry a non-empty fix hint: {bad_row!r}")

    def test_doctor_all_healthy_exits_zero(self):
        good_ref = "vidushi-oa/fastmail:good@x.com"
        self._seed_accounts([
            {"name": "fastmail:good@x.com", "provider": "fastmail",
             "address": "good@x.com", "secret_ref": good_ref},
        ])
        self._seed_secret(good_ref, SENTINEL)

        env = self._doctor_env()
        env["VIDUSHI_FORMAT"] = "toon"
        r = subprocess.run([sys.executable, STORE, "doctor"], capture_output=True, text=True, env=env)

        self.assertEqual(r.returncode, 0,
                          f"all-healthy doctor run must exit 0; stdout={r.stdout!r} stderr={r.stderr!r}")
        self.assertNotIn(SENTINEL, r.stdout)
        self.assertNotIn(SENTINEL, r.stderr)

    def test_doctor_json_mode_is_clean_json_with_no_secret(self):
        good_ref = "vidushi-oa/fastmail:good@x.com"
        self._seed_accounts([
            {"name": "fastmail:good@x.com", "provider": "fastmail",
             "address": "good@x.com", "secret_ref": good_ref},
        ])
        self._seed_secret(good_ref, SENTINEL)

        env = self._doctor_env()
        env["VIDUSHI_FORMAT"] = "json"
        r = subprocess.run([sys.executable, STORE, "doctor", "--json"],
                            capture_output=True, text=True, env=env)

        self.assertEqual(r.returncode, 0, f"stdout={r.stdout!r} stderr={r.stderr!r}")
        payload = json.loads(r.stdout.strip())
        self.assertIsInstance(payload, dict)
        self.assertNotIn(SENTINEL, r.stdout)
        rows = payload.get("accounts")
        self.assertIsInstance(rows, list)
        self.assertEqual(len(rows), 1)
        self.assertIs(rows[0]["resolves"], True)

    def test_doctor_absorbs_setup_check_reporting_store_backend_reachability(self):
        # No accounts configured at all — doctor must still report the store backend +
        # its reachability probe (the behaviour `setup --check` used to own alone).
        env = self._doctor_env()
        env["VIDUSHI_FORMAT"] = "toon"
        r = subprocess.run([sys.executable, STORE, "doctor"], capture_output=True, text=True, env=env)

        self.assertEqual(r.returncode, 0, f"stdout={r.stdout!r} stderr={r.stderr!r}")
        from vidushi_oa import toon as oa_toon
        payload = oa_toon.from_toon(r.stdout)
        store_backend = payload.get("store_backend")
        self.assertIsInstance(store_backend, dict)
        self.assertEqual(store_backend.get("name"), "sqlite")
        self.assertIn("ok", store_backend)
        self.assertIs(store_backend["ok"], True)

    # ------------------------------------------------------------------
    # caller-existence — doctor is a real registered verb
    # ------------------------------------------------------------------

    def test_help_lists_doctor_verb(self):
        r = subprocess.run([sys.executable, STORE, "--help"], capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("doctor", r.stdout, f"--help must list doctor; got:\n{r.stdout}")

    def test_doctor_is_wired_via_a_non_test_set_defaults_caller(self):
        with open(CLI_SRC, encoding="utf-8") as f:
            src = f.read()
        self.assertGreaterEqual(
            src.count("cmd_doctor"), 2,
            f"cmd_doctor must be both defined and wired via a set_defaults caller "
            f"in vidushi_oa/_cli.py (found {src.count('cmd_doctor')} reference(s))",
        )


if __name__ == "__main__":
    unittest.main()
