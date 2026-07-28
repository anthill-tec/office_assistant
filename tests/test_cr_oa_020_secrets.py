"""CR-OA-020 §S4 — pluggable secret resolver (revised for CR-OA-023 §S1).

Per docs/research/DN-mail-access.md Decision 8 (superseding the vault-first
Decision 4), credentials are resolved through a short precedence chain, never
stored by `voa` itself: `voa` holds only a *reference*, the keyring (or file
fallback) holds the secret.

    OS keyring                                             <- PRIMARY
       -> if unavailable / empty
    0600 file                                              <- last resort (warns)

The vault backends (1Password `op` / Bitwarden `bw`) and the `op://` routing were
removed in CR-OA-023 §S1; this suite now exercises only the keyring/file backends
and the reduced precedence chain in `vidushi_oa.mail.secrets`:

  - `SecretBackend(ABC)` — `name`, `available()`, `get(ref)`, `set(ref, value)`.
  - `KeyringBackend` (name="keyring") — via the `keyring` module.
  - `FileBackend` (name="file") — a 0600 JSON file at `VIDUSHI_SECRETS_FILE`.
  - `SecretResolver` — `resolve(ref)` / `store(ref, value)`.

No real OS keyring is touched: `keyring` is pointed at an in-memory fake backend
so no real OS keyring/secret-service is used.
"""
import contextlib
import glob
import io
import os
import stat
import tempfile
import unittest
from abc import ABC

try:
    import keyring
    from keyring.backend import KeyringBackend as _KeyringLibBackend

    _KEYRING_IMPORT_ERROR = None
except ImportError as exc:  # pragma: no cover - environment-dependent
    keyring = None
    _KeyringLibBackend = object
    _KEYRING_IMPORT_ERROR = exc

from vidushi_oa.mail.secrets import (
    FileBackend,
    KeyringBackend,
    SecretBackend,
    SecretResolver,
)


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
    """Shared fixture: env isolation for the keyring/file env vars and (when
    `keyring` is importable) a swapped-in in-memory keyring backend. Everything is
    restored in tearDown."""

    ENV_KEYS = (
        "VIDUSHI_SECRET_BACKEND",
        "VIDUSHI_SECRETS_FILE",
    )

    def setUp(self):
        self._saved_env = {k: os.environ.get(k) for k in self.ENV_KEYS}

        self.secrets_fd, self.secrets_path = tempfile.mkstemp(prefix="oa-cr020-secrets-")
        os.close(self.secrets_fd)
        os.remove(self.secrets_path)  # FileBackend must create it fresh

        os.environ.pop("VIDUSHI_SECRET_BACKEND", None)
        os.environ["VIDUSHI_SECRETS_FILE"] = self.secrets_path

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
            KeyringBackend: "keyring",
            FileBackend: "file",
        }
        for backend_cls, expected_name in expected_names.items():
            with self.subTest(backend=backend_cls.__name__):
                self.assertTrue(issubclass(backend_cls, SecretBackend))
                instance = backend_cls()
                self.assertEqual(instance.name, expected_name)

    def test_each_concrete_backend_available_returns_a_bool(self):
        for backend_cls in (KeyringBackend, FileBackend):
            with self.subTest(backend=backend_cls.__name__):
                self.assertIsInstance(backend_cls().available(), bool)


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


class SecretResolverPrecedenceTest(_SecretsTestBase):
    """§S1 AC: precedence + fallback-with-warning. The primary is the OS keyring;
    when the keyring holds the ref it resolves directly (no warning); when the
    keyring holds nothing, `resolve` falls back to the 0600 file backend (warning
    to stderr); when nothing holds it, it raises LookupError."""

    def test_resolves_from_keyring_primary_without_a_warning(self):
        self._require_keyring()
        ref = "fastmail/token"
        KeyringBackend().set(ref, "keyring-primary-value")

        resolver = SecretResolver()
        captured_err = io.StringIO()
        with contextlib.redirect_stderr(captured_err):
            value = resolver.resolve(ref)

        self.assertEqual(value, "keyring-primary-value")
        self.assertEqual(captured_err.getvalue().strip(), "")

    def test_falls_back_to_file_with_a_stderr_warning_when_keyring_is_empty(self):
        self._require_keyring()
        ref = "fastmail/token"
        # keyring is available (primary) but holds nothing for this ref; only the
        # file backend has the value.
        self.assertIsNone(KeyringBackend().get(ref))
        FileBackend().set(ref, "file-fallback-value")

        resolver = SecretResolver()
        captured_err = io.StringIO()
        with contextlib.redirect_stderr(captured_err):
            value = resolver.resolve(ref)

        self.assertEqual(value, "file-fallback-value")
        warning_text = captured_err.getvalue()
        self.assertTrue(
            warning_text.strip(),
            "expected a fallback warning on stderr when the keyring is empty",
        )
        self.assertNotIn("file-fallback-value", warning_text)

    def test_raises_lookup_error_when_nothing_resolves(self):
        with self.assertRaises(LookupError):
            SecretResolver().resolve("never/configured/anywhere")


class SecretResolverStoreTest(_SecretsTestBase):
    """§S4 AC: `store(ref, value)` writes via the configured primary when it
    supports `set` (keyring/file) and `resolve` reads it back."""

    def test_store_with_file_primary_persists_and_resolve_reads_it_back(self):
        os.environ["VIDUSHI_SECRET_BACKEND"] = "file"
        resolver = SecretResolver()

        resolver.store("fastmail/token", "stored-file-value")

        self.assertEqual(resolver.resolve("fastmail/token"), "stored-file-value")

    def test_store_with_keyring_primary_persists_into_keyring(self):
        self._require_keyring()
        os.environ["VIDUSHI_SECRET_BACKEND"] = "keyring"
        resolver = SecretResolver()

        resolver.store("fastmail/token", "stored-keyring-value")

        self.assertEqual(KeyringBackend().get("fastmail/token"), "stored-keyring-value")


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
