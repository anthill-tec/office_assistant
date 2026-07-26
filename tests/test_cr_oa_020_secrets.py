"""CR-OA-020 §S4 — vault-first pluggable secret resolver (RED).

Per docs/research/DN-mail-access.md Decision 4, credentials are resolved through a
precedence chain, never stored by `voa` itself: `voa` holds only a *reference*, the
vault (or fallback) holds the secret.

    configured vault (1password `op` | bitwarden `bw`)   <- PRIMARY
       -> if missing/token unset/unreachable
    OS keyring                                            <- fallback (warns)
       -> if unavailable
    0600 file                                              <- last resort

`vidushi_oa.mail.secrets` does not exist yet, so every test below fails at import
time with `ModuleNotFoundError` until CR-OA-020's GREEN phase builds:

  - `SecretBackend(ABC)` — `name`, `available()`, `get(ref)`, `set(ref, value)`.
  - `OnePasswordBackend` (name="1password") — `op` CLI, gated on
    `OP_SERVICE_ACCOUNT_TOKEN`; `set()` raises (read-only vault).
  - `BitwardenBackend` (name="bitwarden") — `bw` CLI, gated on `BW_SESSION`;
    `set()` raises (read-only vault).
  - `KeyringBackend` (name="keyring") — via the `keyring` module.
  - `FileBackend` (name="file") — a 0600 JSON file at `VIDUSHI_SECRETS_FILE`.
  - `SecretResolver` — `resolve(ref)` / `store(ref, value)`.

No real vaults/network: fake `op`/`bw` executables are dropped on a temp `PATH`
(tiny shell scripts echoing a canned secret), and `keyring` is pointed at an
in-memory fake backend so no real OS keyring/secret-service is touched.
"""
import contextlib
import glob
import io
import os
import stat
import sys
import tempfile
import unittest
from abc import ABC
from unittest import mock

try:
    import keyring
    from keyring.backend import KeyringBackend as _KeyringLibBackend

    _KEYRING_IMPORT_ERROR = None
except ImportError as exc:  # pragma: no cover - environment-dependent
    keyring = None
    _KeyringLibBackend = object
    _KEYRING_IMPORT_ERROR = exc

from vidushi_oa.mail.secrets import (
    BitwardenBackend,
    FileBackend,
    KeyringBackend,
    OnePasswordBackend,
    SecretBackend,
    SecretResolver,
)

FAKE_OP_SECRET = "fake-1password-secret-testvalue"
FAKE_BW_SECRET = "fake-bitwarden-secret-testvalue"

_OP_SCRIPT = """#!/bin/sh
if [ "$1" = "read" ]; then
  printf '%s\\n' "{secret}"
  exit 0
fi
exit 2
""".format(secret=FAKE_OP_SECRET)

_BW_SCRIPT = """#!/bin/sh
if [ "$1" = "get" ] && [ "$2" = "password" ]; then
  printf '%s\\n' "{secret}"
  exit 0
fi
exit 2
""".format(secret=FAKE_BW_SECRET)


class _InMemoryKeyring(_KeyringLibBackend):
    """A sandboxed, in-process fake `keyring` backend — no real OS keyring/secret
    service is ever touched. Registered via `keyring.set_keyring()`."""

    priority = 1

    def __init__(self):
        super().__init__()
        self._store = {}

    def get_password(self, service, username):
        return self._store.get((service, username))

    def set_password(self, service, username, password):
        self._store[(service, username)] = password

    def delete_password(self, service, username):
        self._store.pop((service, username), None)


class _SecretsTestBase(unittest.TestCase):
    """Shared fixture: a temp bin dir (fake `op`/`bw` on `PATH`), env isolation for
    the vault/keyring/file env vars, and (when `keyring` is importable) a swapped-in
    in-memory keyring backend. Everything is restored in tearDown."""

    ENV_KEYS = (
        "PATH",
        "OP_SERVICE_ACCOUNT_TOKEN",
        "BW_SESSION",
        "VIDUSHI_SECRET_BACKEND",
        "VIDUSHI_SECRETS_FILE",
    )

    def setUp(self):
        self._saved_env = {k: os.environ.get(k) for k in self.ENV_KEYS}

        self.bin_dir = tempfile.mkdtemp(prefix="oa-cr020-fakebin-")
        self._write_fake_exe(os.path.join(self.bin_dir, "op"), _OP_SCRIPT)
        self._write_fake_exe(os.path.join(self.bin_dir, "bw"), _BW_SCRIPT)

        self.empty_bin_dir = tempfile.mkdtemp(prefix="oa-cr020-emptybin-")

        self.secrets_fd, self.secrets_path = tempfile.mkstemp(prefix="oa-cr020-secrets-")
        os.close(self.secrets_fd)
        os.remove(self.secrets_path)  # FileBackend must create it fresh

        os.environ.pop("OP_SERVICE_ACCOUNT_TOKEN", None)
        os.environ.pop("BW_SESSION", None)
        os.environ.pop("VIDUSHI_SECRET_BACKEND", None)
        os.environ["VIDUSHI_SECRETS_FILE"] = self.secrets_path
        os.environ["PATH"] = self.empty_bin_dir

        self._saved_keyring_backend = None
        if keyring is not None:
            try:
                self._saved_keyring_backend = keyring.get_keyring()
                keyring.set_keyring(_InMemoryKeyring())
            except Exception:  # pragma: no cover - environment-dependent
                self._saved_keyring_backend = None

    def tearDown(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if keyring is not None and self._saved_keyring_backend is not None:
            keyring.set_keyring(self._saved_keyring_backend)
        for path in (self.secrets_path,):
            if os.path.exists(path):
                os.remove(path)

    @staticmethod
    def _write_fake_exe(path, content):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.chmod(path, 0o755)

    def _enable_fake_op(self):
        os.environ["PATH"] = self.bin_dir
        os.environ["OP_SERVICE_ACCOUNT_TOKEN"] = "fake-token-abc"

    def _enable_fake_bw(self):
        os.environ["PATH"] = self.bin_dir
        os.environ["BW_SESSION"] = "fake-session-xyz"

    def _require_keyring(self):
        if keyring is None:
            self.skipTest(f"keyring module not importable: {_KEYRING_IMPORT_ERROR}")
        if self._saved_keyring_backend is None:
            self.skipTest("could not sandbox a fake keyring backend in this environment")


class SecretBackendAbstractnessTest(_SecretsTestBase):
    """§S4 AC: `SecretBackend` is the abstract base every concrete backend
    implements — it must not be directly instantiable, and every concrete
    backend must declare the exact `name` the design specifies."""

    def test_secret_backend_is_abstract_base_class(self):
        self.assertTrue(issubclass(SecretBackend, ABC))

    def test_secret_backend_cannot_be_instantiated_directly(self):
        with self.assertRaises(TypeError):
            SecretBackend()

    def test_each_concrete_backend_declares_its_exact_name(self):
        expected_names = {
            OnePasswordBackend: "1password",
            BitwardenBackend: "bitwarden",
            KeyringBackend: "keyring",
            FileBackend: "file",
        }
        for backend_cls, expected_name in expected_names.items():
            with self.subTest(backend=backend_cls.__name__):
                self.assertTrue(issubclass(backend_cls, SecretBackend))
                instance = backend_cls()
                self.assertEqual(instance.name, expected_name)

    def test_each_concrete_backend_available_returns_a_bool(self):
        for backend_cls in (OnePasswordBackend, BitwardenBackend, KeyringBackend, FileBackend):
            with self.subTest(backend=backend_cls.__name__):
                self.assertIsInstance(backend_cls().available(), bool)


class OnePasswordBackendTest(_SecretsTestBase):
    """§S4 AC: `OnePasswordBackend.available()` gates on BOTH the `op` CLI being
    on PATH AND `OP_SERVICE_ACCOUNT_TOKEN` being set; `get()` shells out to
    `op read <ref>` and returns the stripped stdout; `set()` is read-only."""

    def test_unavailable_when_op_cli_missing_and_token_unset(self):
        self.assertFalse(OnePasswordBackend().available())

    def test_unavailable_when_op_cli_present_but_token_unset(self):
        os.environ["PATH"] = self.bin_dir
        self.assertFalse(OnePasswordBackend().available())

    def test_unavailable_when_token_set_but_op_cli_missing(self):
        os.environ["OP_SERVICE_ACCOUNT_TOKEN"] = "fake-token-abc"
        self.assertFalse(OnePasswordBackend().available())

    def test_available_when_op_cli_present_and_token_set(self):
        self._enable_fake_op()
        self.assertTrue(OnePasswordBackend().available())

    def test_get_returns_the_stripped_secret_from_the_fake_op_cli(self):
        self._enable_fake_op()
        value = OnePasswordBackend().get("op://voa-secrets/fastmail/token")
        self.assertEqual(value, FAKE_OP_SECRET)

    def test_set_raises_because_the_vault_is_read_only(self):
        self._enable_fake_op()
        with self.assertRaises(NotImplementedError):
            OnePasswordBackend().set("op://voa-secrets/fastmail/token", "anything")


class BitwardenBackendTest(_SecretsTestBase):
    """§S4 AC: `BitwardenBackend.available()` gates on BOTH the `bw` CLI being
    on PATH AND `BW_SESSION` being set; `set()` is read-only."""

    def test_unavailable_when_bw_cli_missing_and_session_unset(self):
        self.assertFalse(BitwardenBackend().available())

    def test_available_when_bw_cli_present_and_session_set(self):
        self._enable_fake_bw()
        self.assertTrue(BitwardenBackend().available())

    def test_get_returns_the_stripped_secret_from_the_fake_bw_cli(self):
        self._enable_fake_bw()
        value = BitwardenBackend().get("fastmail-token-item")
        self.assertEqual(value, FAKE_BW_SECRET)

    def test_set_raises_because_the_vault_is_read_only(self):
        self._enable_fake_bw()
        with self.assertRaises(NotImplementedError):
            BitwardenBackend().set("fastmail-token-item", "anything")


class FileBackendTest(_SecretsTestBase):
    """§S4 AC: `FileBackend` round-trips `set`/`get` through a JSON file at
    `VIDUSHI_SECRETS_FILE`, and that file is created with mode `0600`."""

    def test_available_is_always_true(self):
        self.assertTrue(FileBackend().available())

    def test_set_then_get_round_trips_the_exact_value(self):
        backend = FileBackend()
        backend.set("fastmail/token", "s3cret-value-42")
        self.assertEqual(backend.get("fastmail/token"), "s3cret-value-42")

    def test_get_returns_none_for_an_unset_ref(self):
        backend = FileBackend()
        self.assertIsNone(backend.get("never/set/this/ref"))

    def test_secrets_file_is_created_with_mode_0600(self):
        backend = FileBackend()
        backend.set("fastmail/token", "s3cret-value-42")

        self.assertTrue(os.path.exists(self.secrets_path))
        mode = stat.S_IMODE(os.stat(self.secrets_path).st_mode)
        self.assertEqual(mode, 0o600)

    def test_two_distinct_refs_do_not_clobber_each_other(self):
        backend = FileBackend()
        backend.set("fastmail/token", "value-one")
        backend.set("gmail/app-password", "value-two")

        self.assertEqual(backend.get("fastmail/token"), "value-one")
        self.assertEqual(backend.get("gmail/app-password"), "value-two")


class KeyringBackendTest(_SecretsTestBase):
    """§S4 AC: `KeyringBackend` round-trips `set`/`get` through the `keyring`
    module — sandboxed here with an in-memory fake backend so no real OS
    keyring/secret-service is ever touched."""

    def test_available_when_keyring_module_imports(self):
        self._require_keyring()
        self.assertTrue(KeyringBackend().available())

    def test_set_then_get_round_trips_the_exact_value(self):
        self._require_keyring()
        backend = KeyringBackend()
        backend.set("fastmail/token", "keyring-value-99")
        self.assertEqual(backend.get("fastmail/token"), "keyring-value-99")

    def test_get_returns_none_for_an_unset_ref(self):
        self._require_keyring()
        backend = KeyringBackend()
        self.assertIsNone(backend.get("never/set/this/ref"))


class SecretResolverOnePasswordRefTest(_SecretsTestBase):
    """§S4 AC: a ref starting with `op://` is ALWAYS routed to the 1Password
    backend regardless of the configured primary — verified both by the
    returned value and by spying on `OnePasswordBackend.get` to confirm it (and
    only it) was called with the exact ref."""

    def test_resolve_of_an_op_ref_returns_the_fake_secret_via_1password_backend(self):
        self._enable_fake_op()
        ref = "op://voa-secrets/fastmail/token"
        resolver = SecretResolver()

        original_get = OnePasswordBackend.get
        with mock.patch.object(
            OnePasswordBackend, "get", autospec=True, side_effect=original_get
        ) as spy:
            value = resolver.resolve(ref)

        self.assertEqual(value, FAKE_OP_SECRET)
        spy.assert_called_once_with(mock.ANY, ref)


class SecretResolverPrecedenceTest(_SecretsTestBase):
    """§S4 AC: precedence + fallback-with-warning. With the configured vault
    primary UNAVAILABLE, `resolve(ref)` falls back to keyring (warning to
    stderr); with keyring holding nothing for that ref either, it falls
    further to the file backend."""

    def test_falls_back_to_keyring_with_a_stderr_warning_when_primary_vault_unavailable(self):
        self._require_keyring()
        os.environ["VIDUSHI_SECRET_BACKEND"] = "1password"
        # op CLI stays off PATH / token unset -> OnePasswordBackend.available() is False.
        self.assertFalse(OnePasswordBackend().available())

        ref = "fastmail/token"
        KeyringBackend().set(ref, "keyring-fallback-value")

        resolver = SecretResolver()
        captured_err = io.StringIO()
        with contextlib.redirect_stderr(captured_err):
            value = resolver.resolve(ref)

        self.assertEqual(value, "keyring-fallback-value")
        warning_text = captured_err.getvalue()
        self.assertTrue(
            warning_text.strip(),
            "expected a fallback warning on stderr when the primary vault is unavailable",
        )
        self.assertNotIn("keyring-fallback-value", warning_text)

    def test_falls_back_to_file_backend_when_primary_and_keyring_both_have_nothing(self):
        self._require_keyring()
        os.environ["VIDUSHI_SECRET_BACKEND"] = "1password"
        self.assertFalse(OnePasswordBackend().available())

        ref = "fastmail/token"
        # keyring is available but holds nothing for this ref; only the file
        # backend has the value.
        self.assertIsNone(KeyringBackend().get(ref))
        FileBackend().set(ref, "file-fallback-value")

        resolver = SecretResolver()
        value = resolver.resolve(ref)

        self.assertEqual(value, "file-fallback-value")

    def test_raises_lookup_error_when_nothing_resolves(self):
        os.environ["VIDUSHI_SECRET_BACKEND"] = "1password"
        self.assertFalse(OnePasswordBackend().available())

        with self.assertRaises(LookupError):
            SecretResolver().resolve("never/configured/anywhere")

    def test_auto_primary_prefers_1password_over_bitwarden_when_both_available(self):
        self._enable_fake_op()
        self._enable_fake_bw()
        os.environ["BW_SESSION"] = "fake-session-xyz"

        resolver = SecretResolver()
        value = resolver.resolve("op://voa-secrets/fastmail/token")

        self.assertEqual(value, FAKE_OP_SECRET)


class SecretResolverStoreTest(_SecretsTestBase):
    """§S4 AC: `store(ref, value)` writes via the primary when it supports
    `set` (keyring/file); a read-only vault primary falls back to keyring/file
    since `voa` cannot write into the vault."""

    def test_store_with_file_primary_persists_and_resolve_reads_it_back(self):
        os.environ["VIDUSHI_SECRET_BACKEND"] = "file"
        resolver = SecretResolver()

        resolver.store("fastmail/token", "stored-file-value")

        self.assertEqual(resolver.resolve("fastmail/token"), "stored-file-value")

    def test_store_falls_back_to_keyring_or_file_when_primary_is_read_only_vault(self):
        self._require_keyring()
        self._enable_fake_op()
        os.environ["VIDUSHI_SECRET_BACKEND"] = "1password"
        resolver = SecretResolver()

        # 1Password is a read-only vault: store() must not attempt op writes,
        # and must fall through to keyring or file instead.
        resolver.store("fastmail/token", "stored-fallback-value")

        stored_in_keyring = KeyringBackend().get("fastmail/token")
        stored_in_file = FileBackend().get("fastmail/token")
        self.assertIn("stored-fallback-value", (stored_in_keyring, stored_in_file))


class SecretNeverPersistedTest(_SecretsTestBase):
    """§S4 AC: the raw secret value never leaks into the store DB, `data/*.jsonl`,
    any config the resolver writes, or captured stdout/stderr — only the file
    backend's own designated 0600 store (exempt, since that IS the secret
    store) may contain it."""

    def test_sentinel_secret_never_appears_in_jsonl_snapshots_or_captured_logs(self):
        sentinel = "SENTINEL-9f3c7e2a-never-leak"
        os.environ["VIDUSHI_SECRET_BACKEND"] = "file"
        resolver = SecretResolver()

        captured_out, captured_err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(captured_out), contextlib.redirect_stderr(captured_err):
            resolver.store("fastmail/token", sentinel)
            resolved_value = resolver.resolve("fastmail/token")

        self.assertEqual(resolved_value, sentinel)
        self.assertNotIn(sentinel, captured_out.getvalue())
        self.assertNotIn(sentinel, captured_err.getvalue())

        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for jsonl_path in glob.glob(os.path.join(project_root, "data", "*.jsonl")):
            with open(jsonl_path, "r", encoding="utf-8") as fh:
                self.assertNotIn(sentinel, fh.read())


if __name__ == "__main__":
    unittest.main()
