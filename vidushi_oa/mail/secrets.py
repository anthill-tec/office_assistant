"""Vault-first pluggable secret resolver (CR-OA-020 §S4).

Per ``docs/research/DN-mail-access.md`` Decision 4, ``voa`` never stores mail
credentials itself: it holds only a *reference*, and the secret lives in a vault
(1Password / Bitwarden) or, failing that, in the OS keyring, or, as a last
resort, in a ``0600`` JSON file. Resolution walks a precedence chain::

    configured vault (1password `op` | bitwarden `bw`)   <- PRIMARY
       -> if missing / token unset / unreachable
    OS keyring                                            <- fallback (warns)
       -> if unavailable / empty
    0600 file                                             <- last resort

A ``op://`` reference is always routed to 1Password regardless of the configured
primary. The raw secret value is never logged, printed, or written anywhere but
the file backend's own designated ``0600`` store.
"""
import json
import os
import shutil
import subprocess
import sys
from abc import ABC, abstractmethod

try:  # keyring is an optional (`mail` extra) dependency.
    import keyring as _keyring
except ImportError:  # pragma: no cover - environment-dependent
    _keyring = None

KEYRING_SERVICE = "vidushi-oa"
SECRETS_FILE_ENV = "VIDUSHI_SECRETS_FILE"
BACKEND_ENV = "VIDUSHI_SECRET_BACKEND"


class SecretBackend(ABC):
    """Abstract base every concrete secret backend implements."""

    #: The stable identifier of this backend (overridden by subclasses).
    name: str = ""

    @abstractmethod
    def available(self) -> bool:
        """Return True when this backend can be used in the current environment."""

    @abstractmethod
    def get(self, ref: str) -> str | None:
        """Return the secret for ``ref``, or None if this backend holds none."""

    def set(self, ref: str, value: str) -> None:
        """Persist ``value`` under ``ref``. Read-only backends leave this raising."""
        raise NotImplementedError(f"{self.name} backend is read-only")


class OnePasswordBackend(SecretBackend):
    """1Password backend via the ``op`` CLI (a read-only vault for ``voa``)."""

    name = "1password"

    def available(self) -> bool:
        return bool(shutil.which("op")) and bool(os.environ.get("OP_SERVICE_ACCOUNT_TOKEN"))

    def get(self, ref: str) -> str | None:
        result = subprocess.run(
            ["op", "read", ref], capture_output=True, text=True
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip()


class BitwardenBackend(SecretBackend):
    """Bitwarden backend via the ``bw`` CLI (a read-only vault for ``voa``)."""

    name = "bitwarden"

    def available(self) -> bool:
        return bool(shutil.which("bw")) and bool(os.environ.get("BW_SESSION"))

    def get(self, ref: str) -> str | None:
        session = os.environ.get("BW_SESSION", "")
        result = subprocess.run(
            ["bw", "get", "password", ref, "--session", session],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip()


class KeyringBackend(SecretBackend):
    """OS keyring backend via the ``keyring`` module (writable)."""

    name = "keyring"

    def available(self) -> bool:
        return _keyring is not None

    def get(self, ref: str) -> str | None:
        if _keyring is None:  # pragma: no cover - environment-dependent
            return None
        return _keyring.get_password(KEYRING_SERVICE, ref)

    def set(self, ref: str, value: str) -> None:
        if _keyring is None:  # pragma: no cover - environment-dependent
            raise NotImplementedError("keyring module is not installed")
        _keyring.set_password(KEYRING_SERVICE, ref, value)


class FileBackend(SecretBackend):
    """Last-resort backend: a ``0600`` JSON file (writable)."""

    name = "file"

    def available(self) -> bool:
        return True

    def _path(self) -> str:
        configured = os.environ.get(SECRETS_FILE_ENV)
        if configured:
            return configured
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
            os.path.expanduser("~"), ".config"
        )
        return os.path.join(base, "vidushi-oa", "secrets.json")

    def _load(self) -> dict:
        path = self._path()
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def get(self, ref: str) -> str | None:
        return self._load().get(ref)

    def set(self, ref: str, value: str) -> None:
        path = self._path()
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        data = self._load()
        data[ref] = value
        # Create with 0600 up front so the secret is never briefly world-readable.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.chmod(path, 0o600)


class SecretResolver:
    """Resolve / store secret references through the precedence chain."""

    def _backend_by_name(self, name: str) -> SecretBackend:
        backends = {
            "1password": OnePasswordBackend,
            "bitwarden": BitwardenBackend,
            "keyring": KeyringBackend,
            "file": FileBackend,
        }
        cls = backends.get(name)
        if cls is None:
            raise ValueError(f"unknown secret backend: {name!r}")
        return cls()

    def _primary_backend(self) -> SecretBackend:
        configured = os.environ.get(BACKEND_ENV)
        if configured:
            return self._backend_by_name(configured)
        for candidate in (OnePasswordBackend(), BitwardenBackend(), KeyringBackend()):
            if candidate.available():
                return candidate
        return FileBackend()

    def _resolution_chain(self) -> list[SecretBackend]:
        chain: list[SecretBackend] = [self._primary_backend()]
        seen = {chain[0].name}
        for backend in (KeyringBackend(), FileBackend()):
            if backend.name not in seen:
                chain.append(backend)
                seen.add(backend.name)
        return chain

    def resolve(self, ref: str) -> str:
        if ref.startswith("op://"):
            value = OnePasswordBackend().get(ref)
            if value is not None:
                return value
            raise LookupError(f"1Password could not resolve ref {ref!r}")

        chain = self._resolution_chain()
        primary = chain[0]
        for backend in chain:
            if not backend.available():
                continue
            value = backend.get(ref)
            if value is not None:
                if backend is not primary:
                    sys.stderr.write(
                        f"vidushi-oa: primary secret backend '{primary.name}' "
                        f"unavailable or empty for ref {ref!r}; "
                        f"fell back to '{backend.name}'.\n"
                    )
                return value
        raise LookupError(f"no secret backend could resolve ref {ref!r}")

    def store(self, ref: str, value: str) -> None:
        primary = self._primary_backend()
        try:
            primary.set(ref, value)
            return
        except NotImplementedError:
            pass
        keyring_backend = KeyringBackend()
        if keyring_backend.available():
            keyring_backend.set(ref, value)
            return
        FileBackend().set(ref, value)
