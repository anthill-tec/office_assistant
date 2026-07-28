"""Keyring-primary pluggable secret resolver (CR-OA-023 §S1).

Per ``docs/research/DN-mail-access.md`` Decision 8, ``voa`` never stores mail
credentials itself: it holds only a *reference*, and the secret lives in the OS
keyring or, as a last resort, in a ``0600`` JSON file. Resolution walks a short
precedence chain::

    OS keyring                                            <- PRIMARY
       -> if unavailable / empty
    0600 file                                             <- last resort (warns)

``VIDUSHI_SECRET_BACKEND`` may pin the primary explicitly to ``keyring`` or
``file``; any other name is rejected. The raw secret value is never logged,
printed, or written anywhere but the file backend's own designated ``0600`` store.
"""
import json
import os
import sys
import uuid
from abc import ABC, abstractmethod

try:  # keyring is a base dependency (CR-OA-023 §S2), but stay defensive.
    import keyring as _keyring
    from keyring.errors import PasswordDeleteError as _PasswordDeleteError
except ImportError:  # pragma: no cover - environment-dependent
    _keyring = None

    class _PasswordDeleteError(Exception):  # noqa: N818 - keyring-parity name
        """Placeholder used when the keyring module is unavailable."""

KEYRING_SERVICE = "vidushi-oa"
SECRETS_FILE_ENV = "VIDUSHI_SECRETS_FILE"
BACKEND_ENV = "VIDUSHI_SECRET_BACKEND"
DESKTOP_ENV = "XDG_CURRENT_DESKTOP"


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

    def _save(self, data: dict) -> None:
        path = self._path()
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # Create with 0600 up front so the secret is never briefly world-readable.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.chmod(path, 0o600)

    def set(self, ref: str, value: str) -> None:
        data = self._load()
        data[ref] = value
        self._save(data)

    def delete(self, ref: str) -> None:
        """Remove ``ref`` if present (used by the pre-flight cleanup)."""
        data = self._load()
        if ref in data:
            data.pop(ref)
            self._save(data)


class SecretResolver:
    """Resolve / store secret references through the precedence chain."""

    def _backend_by_name(self, name: str) -> SecretBackend:
        backends = {
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
        keyring_backend = KeyringBackend()
        if keyring_backend.available():
            return keyring_backend
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


# --- CR-OA-023 §S3: OS-aware secret provisioning + pre-flight -----------------

def detect_desktop() -> str:
    """Coarse host desktop / Secret-Service provider hint.

    Returns one of ``"macos"`` (native login Keychain), ``"kde"``, ``"gnome"``,
    ``"other"`` (a known freedesktop desktop) or ``"headless"`` (no desktop
    advertised). Pure: reads only ``sys.platform`` and ``XDG_CURRENT_DESKTOP``
    so it is directly unit-coverable.
    """
    if sys.platform == "darwin":
        return "macos"
    desktop = (os.environ.get(DESKTOP_ENV) or "").strip().lower()
    if not desktop:
        return "headless"
    if "kde" in desktop:
        return "kde"
    if "gnome" in desktop:
        return "gnome"
    return "other"


def keyring_guidance(desktop: str) -> str:
    """Human-readable remedy naming the OS-specific Secret-Service provider.

    Every branch also names the explicit ``--secret-backend file`` escape hatch
    so reaching the file backend stays a stated, confirmed choice (§S3 / DN
    Decision 8) rather than a silent downgrade.
    """
    if desktop == "kde":
        return ("No Secret Service provider is reachable. On KDE, enable KWallet's "
                "Secret Service integration so it claims 'org.freedesktop.secrets', "
                "then re-run setup -- or choose the file backend explicitly with "
                "'--secret-backend file'.")
    if desktop == "gnome":
        return ("No Secret Service provider is reachable. On GNOME/freedesktop, install "
                "and run gnome-keyring (libsecret) so it provides "
                "'org.freedesktop.secrets', then re-run setup -- or choose the file "
                "backend explicitly with '--secret-backend file'.")
    if desktop == "macos":
        return ("The macOS login Keychain should be available with no action; if the "
                "keyring is unreachable, choose the file backend explicitly with "
                "'--secret-backend file'.")
    return ("No Secret Service provider is reachable (headless or unknown desktop). "
            "Choose the file backend explicitly with '--secret-backend file' to store "
            "the secret in a 0600 file as a stated, confirmed choice.")


def _cleanup_preflight(backend: SecretBackend, ref: str) -> None:
    """Best-effort removal of the throwaway pre-flight ref (never fatal)."""
    if isinstance(backend, KeyringBackend) and _keyring is not None:
        try:
            _keyring.delete_password(KEYRING_SERVICE, ref)
        except _PasswordDeleteError as exc:  # nothing to delete / unsupported
            sys.stderr.write(
                f"vidushi-oa: pre-flight cleanup could not remove {ref!r}: {exc}\n")
    elif isinstance(backend, FileBackend):
        backend.delete(ref)


def preflight(backend: SecretBackend) -> dict:
    """Perform a ``set``->``get`` round-trip on ``backend`` with a throwaway ref.

    Returns ``{"ok": True}`` when the value round-trips, else
    ``{"ok": False, "error": <exception type name>}``. A backend whose
    ``set``/``get`` raises for ANY reason (no provider reachable, read-only, a
    backend error) is reported as ``ok: False`` -- the breadth of the catch is
    intentional per §S3 (the round-trip is exactly the reachability probe).
    """
    ref = "vidushi-oa-preflight-" + uuid.uuid4().hex
    token = uuid.uuid4().hex
    try:
        backend.set(ref, token)
        got = backend.get(ref)
    except Exception as exc:  # noqa: BLE001 - any failure -> preflight not ok (§S3)
        return {"ok": False, "error": type(exc).__name__}
    _cleanup_preflight(backend, ref)
    return {"ok": bool(got == token)}
