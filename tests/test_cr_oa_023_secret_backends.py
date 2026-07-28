"""CR-OA-023 §S1 — remove the vault backends; resolver reduces to keyring -> file.

Per docs/changes/CR-OA-023-keyring-primary-os-aware-secret-setup.md §S1, CR-OA-020's
vault-first precedence chain (1Password `op` / Bitwarden `bw` PRIMARY -> keyring ->
0600 file) is dropped: `OnePasswordBackend`, `BitwardenBackend`, the `op://` special
routing, and their `VIDUSHI_SECRET_BACKEND` registry entries + auto-detect are
removed from `vidushi_oa/mail/secrets.py`; both symbols also disappear from
`vidushi_oa/mail/__init__.py`'s exports. The precedence chain becomes
**keyring (primary) -> 0600 file (last resort)** only.

Every test below currently FAILS against `vidushi_oa/mail/secrets.py` because the
vault backends and their `op://` fast-path/registry entries still exist there:

  - `OnePasswordBackend` / `BitwardenBackend` are still importable from
    `vidushi_oa.mail` (post-removal, importing either name must raise
    `ImportError`).
  - `SecretResolver()._backend_by_name("1password" | "bitwarden")` still returns a
    vault backend instance instead of raising `ValueError: unknown secret backend`.
  - `SecretResolver()._primary_backend()` still auto-detects / honours a
    configured vault backend ahead of keyring (must reduce to keyring/file only).
  - an `op://` ref is still specially routed straight to `OnePasswordBackend` and
    raises the vault-specific `"1Password could not resolve"` message when
    absent, instead of falling through the normal keyring->file chain and
    raising the generic `"no secret backend could resolve ref"` message.
  - the vault class names / `op://` literal still appear in `vidushi_oa/` source.

No real vaults/network/OS keyring are touched: a fake `op` executable is dropped on
a temp `PATH` only where a test needs `OnePasswordBackend.available()` to read True
(to prove today's vault-first precedence, pre-removal), and keyring availability is
toggled deterministically by patching the module's `_keyring` reference directly
rather than depending on a real Secret Service being present in CI.
"""
import os
import re
import tempfile
import unittest
from unittest import mock

from vidushi_oa.mail import secrets as secrets_module
from vidushi_oa.mail.secrets import SecretResolver

_OP_SCRIPT = """#!/bin/sh
if [ "$1" = "read" ]; then
  printf '%s\\n' "fake-1password-secret"
  exit 0
fi
exit 2
"""


class _SecretBackendsTestBase(unittest.TestCase):
    """Env isolation for the vault/keyring/file env vars + a temp PATH so no real
    ``op``/``bw`` CLI or OS keyring/secret-service is ever touched."""

    ENV_KEYS = (
        "PATH",
        "OP_SERVICE_ACCOUNT_TOKEN",
        "BW_SESSION",
        "VIDUSHI_SECRET_BACKEND",
        "VIDUSHI_SECRETS_FILE",
    )

    def setUp(self):
        self._saved_env = {k: os.environ.get(k) for k in self.ENV_KEYS}

        self.empty_bin_dir = tempfile.mkdtemp(prefix="oa-cr023-emptybin-")
        self.fake_op_bin_dir = tempfile.mkdtemp(prefix="oa-cr023-fakeop-")
        op_path = os.path.join(self.fake_op_bin_dir, "op")
        with open(op_path, "w", encoding="utf-8") as fh:
            fh.write(_OP_SCRIPT)
        os.chmod(op_path, 0o755)

        self.secrets_fd, self.secrets_path = tempfile.mkstemp(prefix="oa-cr023-secrets-")
        os.close(self.secrets_fd)
        os.remove(self.secrets_path)  # FileBackend must create it fresh

        os.environ.pop("OP_SERVICE_ACCOUNT_TOKEN", None)
        os.environ.pop("BW_SESSION", None)
        os.environ.pop("VIDUSHI_SECRET_BACKEND", None)
        os.environ["VIDUSHI_SECRETS_FILE"] = self.secrets_path
        os.environ["PATH"] = self.empty_bin_dir

    def tearDown(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if os.path.exists(self.secrets_path):
            os.remove(self.secrets_path)

    def _enable_fake_op(self):
        """Make a vault "look available" (fake `op` CLI on PATH + token set) —
        used to prove no code path prefers it over keyring any more."""
        os.environ["PATH"] = self.fake_op_bin_dir
        os.environ["OP_SERVICE_ACCOUNT_TOKEN"] = "fake-token-abc"

    @staticmethod
    def _patch_keyring_available():
        """Make `KeyringBackend.available()` True without touching a real OS
        keyring/secret-service."""
        return mock.patch.object(secrets_module, "_keyring", mock.Mock())

    @staticmethod
    def _patch_keyring_unavailable():
        return mock.patch.object(secrets_module, "_keyring", None)


class VaultSymbolsRemovedTest(_SecretBackendsTestBase):
    """§S1 AC: `OnePasswordBackend`/`BitwardenBackend` no longer exist —
    importing either name from `vidushi_oa.mail` raises `ImportError`."""

    def test_importing_onepassword_backend_from_mail_package_raises_import_error(self):
        with self.assertRaises(ImportError):
            from vidushi_oa.mail import OnePasswordBackend  # noqa: F401

    def test_importing_bitwarden_backend_from_mail_package_raises_import_error(self):
        with self.assertRaises(ImportError):
            from vidushi_oa.mail import BitwardenBackend  # noqa: F401


class RegistryRejectsVaultNamesTest(_SecretBackendsTestBase):
    """§S1 AC: the backend-name registry has no `1password`/`bitwarden` entries —
    `_backend_by_name` raises `ValueError: unknown secret backend` for both, and a
    configured `VIDUSHI_SECRET_BACKEND` of either name propagates that same error
    out of `_primary_backend()` rather than yielding a vault instance."""

    def test_backend_by_name_rejects_1password_as_unknown(self):
        with self.assertRaises(ValueError) as ctx:
            SecretResolver()._backend_by_name("1password")
        self.assertIn("unknown secret backend", str(ctx.exception))

    def test_backend_by_name_rejects_bitwarden_as_unknown(self):
        with self.assertRaises(ValueError) as ctx:
            SecretResolver()._backend_by_name("bitwarden")
        self.assertIn("unknown secret backend", str(ctx.exception))

    def test_configured_backend_env_of_1password_raises_value_error_not_a_vault(self):
        os.environ["VIDUSHI_SECRET_BACKEND"] = "1password"
        with self.assertRaises(ValueError) as ctx:
            SecretResolver()._primary_backend()
        self.assertIn("unknown secret backend", str(ctx.exception))

    def test_configured_backend_env_of_bitwarden_raises_value_error_not_a_vault(self):
        os.environ["VIDUSHI_SECRET_BACKEND"] = "bitwarden"
        with self.assertRaises(ValueError) as ctx:
            SecretResolver()._primary_backend()
        self.assertIn("unknown secret backend", str(ctx.exception))


class PrimaryBackendReducedToKeyringOrFileTest(_SecretBackendsTestBase):
    """§S1 AC: with a keyring backend available, `_primary_backend().name ==
    "keyring"`; with none available it is `"file"`. There is no code path that
    selects a vault backend ahead of keyring — proven by making a vault "look
    available" (a fake `op` CLI present + its token set) and asserting it is
    still never chosen."""

    def test_primary_is_keyring_when_available_even_with_a_vault_looking_available(self):
        self._enable_fake_op()
        with self._patch_keyring_available():
            name = SecretResolver()._primary_backend().name
        self.assertEqual(name, "keyring")

    def test_primary_is_file_when_keyring_unavailable_even_with_a_vault_looking_available(self):
        self._enable_fake_op()
        with self._patch_keyring_unavailable():
            name = SecretResolver()._primary_backend().name
        self.assertEqual(name, "file")


class OpRefNoLongerSpeciallyRoutedTest(_SecretBackendsTestBase):
    """§S1 AC: a ref of the form `op://...` is not specially routed to a
    1Password backend any more — it resolves through the normal keyring->file
    chain, and when no backend holds it, raises the STANDARD not-found error
    (not the vault-specific "1Password could not resolve" message)."""

    def test_op_ref_raises_the_generic_chain_not_found_error_when_absent(self):
        self._enable_fake_op()  # a vault "looking available" must not intercept this
        with self._patch_keyring_unavailable():
            with self.assertRaises(LookupError) as ctx:
                SecretResolver().resolve("op://Vault/item")
        message = str(ctx.exception)
        self.assertIn("no secret backend could resolve ref", message)
        self.assertNotIn("1Password", message)

    def test_op_ref_resolves_through_keyring_when_keyring_holds_it(self):
        with self._patch_keyring_available() as mock_keyring:
            mock_keyring.get_password.return_value = "keyring-op-value"
            value = SecretResolver().resolve("op://Vault/item")
        self.assertEqual(value, "keyring-op-value")
        mock_keyring.get_password.assert_called_once_with(
            secrets_module.KEYRING_SERVICE, "op://Vault/item"
        )


class NoVaultReferencesRemainInSourceTest(unittest.TestCase):
    """§S1 AC: `grep -rE "OnePasswordBackend|BitwardenBackend|op://" vidushi_oa/`
    returns zero matches."""

    def test_no_vault_references_remain_anywhere_in_vidushi_oa_source(self):
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        package_dir = os.path.join(project_root, "vidushi_oa")
        pattern = re.compile(r"OnePasswordBackend|BitwardenBackend|op://")
        offending = []
        for dirpath, _dirnames, filenames in os.walk(package_dir):
            for filename in filenames:
                if not filename.endswith(".py"):
                    continue
                path = os.path.join(dirpath, filename)
                with open(path, "r", encoding="utf-8") as fh:
                    content = fh.read()
                if pattern.search(content):
                    offending.append(os.path.relpath(path, project_root))
        self.assertEqual(offending, [], f"vault references still present in: {offending}")


if __name__ == "__main__":
    unittest.main()
