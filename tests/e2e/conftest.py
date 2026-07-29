"""Session-scoped Dockerized-emulator fixture for the LOCAL mail E2E tier.

Design of record: ``docs/research/DN-mail-e2e-emulator-testing.md`` (Decisions 1, 2, 4
and "Borrowed Docker configs"). This module spins up ONE throwaway Stalwart mail server
(`stalwartlabs/mail-server`) via ``testcontainers``, seeds THREE provider profiles that
mimic each provider's observable behaviour, yields their connection details, and tears the
container down after the session.

Nothing here runs in CI. The whole tier is excluded from the default test population by
``addopts = -m "not e2e"`` (pyproject) and additionally auto-skips when Docker or the
``[e2e]`` extra is absent (the ``stalwart_emulator`` fixture below). Seeding uses only the
stdlib (``imaplib`` / ``smtplib`` / ``urllib`` / ``socket``) so the sole extra dependency is
``testcontainers`` itself.

The three profiles (Decision 2):

* ``fastmail`` — a JMAP account. Its session resource is Stalwart's ``/.well-known/jmap``;
  the account authenticates with ``Authorization: Bearer <base64(address:password)>`` — which
  is exactly what ``JmapAdapter`` emits, so the emulator's ``token`` field is that base64
  blob and ``jmap_url`` is the well-known URL. Validates the JMAP send/draft path.
* ``gmail`` — an IMAP+SMTP account shaped like Gmail: its Drafts folder is renamed to
  ``[Gmail]/Drafts`` (keeping the RFC 6154 ``\\Drafts`` special-use role) and a per-account
  Sieve script (uploaded over ManageSieve) files a self-submitted message into ``Sent``,
  reproducing Gmail's server-side auto-file-of-sent so the skip-the-Sent-APPEND branch is
  validated end-to-end.
* ``yahoo`` — a plain RFC 3501 IMAP+SMTP account with standard folder names.
"""
import base64
import imaplib
import json
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

import pytest

# ---------------------------------------------------------------------------
# Configuration knobs
# ---------------------------------------------------------------------------
STALWART_IMAGE = "stalwartlabs/mail-server:v0.11.8-alpine"
# A non-reserved TLD on purpose: Stalwart rejects the RFC 2606 `.test` TLD as an
# "invalid e-mail address" for a JMAP Identity (and auto-creates none for it), which
# blocks `EmailSubmission/set` — RFC 8621 §7.1 makes `identityId` required, and the
# JMAP send path resolves it via `Identity/get`. With a normal TLD Stalwart
# auto-provisions the account's identity, so the fastmail JMAP send round-trip works.
EMU_DOMAIN = "emumail.org"
# submission / IMAP / JMAP-HTTP-admin+API + ManageSieve (the last is needed ONLY to seed the
# gmail per-account Sieve script). Seeding + test assertions use PLAINTEXT IMAP 143; the
# real `voa` IMAP round-trip uses IMAPS 993 (implicit TLS) because `ImapAdapter` dials
# `imaplib.IMAP4_SSL` — a NON-verifying stdlib context (CERT_NONE), so Stalwart's
# self-signed cert is accepted with no CA/trust-store setup. SMTP 587 carries STARTTLS.
PORT_HTTP = 8080
PORT_IMAP = 143
PORT_IMAPS = 993
PORT_SMTP = 587
PORT_SIEVE = 4190

# Seeded accounts: name -> (provider, local password). Addresses are <name-stem>@emu.test.
_ACCOUNTS = {
    "fastmail": ("fastmail", "fastmailpass"),
    "gmail": ("gmail", "gmailpass"),
    "yahoo": ("yahoo", "yahoopass"),
}

# Per-account Sieve for the gmail profile: file a message the account sent to itself into
# "Sent", mirroring Gmail's server-side sent-filing (so our send path need not APPEND).
_GMAIL_SIEVE = (
    'require ["fileinto"];\n'
    f'if header :contains "from" "gmail@{EMU_DOMAIN}" {{ fileinto "Sent"; }}\n'
)


@dataclass
class Profile:
    """Everything a test needs to point ``voa`` at one emulator account: build the
    ``VIDUSHI_MAIL_ENDPOINTS`` override + register via ``voa mail-auth --send --endpoint``."""

    name: str            # account name registered with voa, e.g. "fastmail@emu.test"
    provider: str        # "fastmail" | "gmail" | "yahoo"
    host: str
    address: str
    password: str
    imap_port: int       # PLAINTEXT IMAP (143) — seeding + in-test assertions
    smtp_port: int       # submission (587) with STARTTLS — the voa send path
    imaps_port: int = 0  # IMAPS (993, implicit TLS) — the voa IMAP round-trip path
    jmap_url: str = ""   # fastmail only — the JMAP session resource URL
    token: str = ""      # fastmail only — the Bearer token voa registers (base64 addr:pass)

    def endpoint(self) -> dict:
        """The ``endpoint`` override object (Decision 3) for this profile.

        The IMAP override points at the IMAPS (993) implicit-TLS port, not plaintext
        143: ``ImapAdapter`` dials ``imaplib.IMAP4_SSL``. ``tls_verify: false`` is the
        explicit opt-out the emulator needs: the adapter now verifies the server
        certificate/hostname by default, and Stalwart presents a self-signed cert, so
        without the opt-out the IMAP/SMTP TLS handshake would be rejected."""
        if self.provider == "fastmail":
            return {"jmap_url": self.jmap_url}
        return {"imap_host": self.host, "imap_port": self.imaps_port,
                "smtp_host": self.host, "smtp_port": self.smtp_port,
                "tls_verify": False}


@dataclass
class Emulator:
    """Handle onto the running Stalwart container + its seeded profiles."""

    host: str
    http_port: int
    admin_user: str
    admin_password: str
    container_ip: str = ""   # docker bridge IP — reachable from the host on native ports
    profiles: dict = field(default_factory=dict)

    @property
    def admin_base(self) -> str:
        return f"http://{self.host}:{self.http_port}"


# ---------------------------------------------------------------------------
# Stalwart admin API (Basic-auth JSON over HTTP)
# ---------------------------------------------------------------------------
def _admin_call(base, auth_header, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Authorization": auth_header, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except (ValueError, OSError):
            return e.code, None


def _wait_for_admin(base, auth_header, timeout=60):
    """Block until the admin API answers a real query (listeners + store are up)."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            status, _ = _admin_call(base, auth_header, "GET", "/api/principal?limit=1")
            if status == 200:
                return
            last = status
        except (urllib.error.URLError, OSError) as e:
            # A starting-up HTTP listener refuses/resets connections (ConnectionResetError,
            # ConnectionRefusedError — both OSError, not URLError) until it is ready.
            last = e
        time.sleep(0.5)
    raise RuntimeError(f"Stalwart admin API not ready within {timeout}s (last={last!r})")


# ---------------------------------------------------------------------------
# Minimal ManageSieve client (PUTSCRIPT + SETACTIVE over SASL PLAIN)
# ---------------------------------------------------------------------------
class _ManageSieve:
    def __init__(self, host, port, timeout=15):
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._fp = self._sock.makefile("rb")
        self._drain_until_status()

    def _drain_until_status(self):
        chunks = []
        while True:
            line = self._fp.readline()
            if not line:
                break
            chunks.append(line)
            if line[:2] in (b"OK", b"NO") or line[:3] == b"BYE":
                break
        return b"".join(chunks)

    def _command(self, line):
        self._sock.sendall(line.encode() + b"\r\n")
        return self._drain_until_status()

    def authenticate(self, user, password):
        token = base64.b64encode(f"\x00{user}\x00{password}".encode()).decode()
        resp = self._command(f'AUTHENTICATE "PLAIN" "{token}"')
        if not resp.startswith(b"OK"):
            raise RuntimeError(f"ManageSieve auth failed: {resp!r}")

    def put_and_activate(self, name, script):
        body = script.encode()
        self._sock.sendall(f'PUTSCRIPT "{name}" {{{len(body)}+}}\r\n'.encode()
                           + body + b"\r\n")
        resp = self._drain_until_status()
        if not resp.startswith(b"OK"):
            raise RuntimeError(f"ManageSieve PUTSCRIPT failed: {resp!r}")
        resp = self._command(f'SETACTIVE "{name}"')
        if not resp.startswith(b"OK"):
            raise RuntimeError(f"ManageSieve SETACTIVE failed: {resp!r}")

    def close(self):
        try:
            self._command("LOGOUT")
        finally:
            self._sock.close()


# ---------------------------------------------------------------------------
# IMAP helpers (SASL PLAIN — the emulator is configured for cleartext auth)
# ---------------------------------------------------------------------------
def _imap_login(host, port, address, password, retries=10):
    last = None
    for _ in range(retries):
        try:
            conn = imaplib.IMAP4(host, port)
            conn.authenticate("PLAIN",
                              lambda _c, a=address, p=password: f"\x00{a}\x00{p}".encode())
            return conn
        except (imaplib.IMAP4.error, OSError) as e:
            last = e
            time.sleep(0.5)
    raise RuntimeError(f"IMAP login to {address} failed: {last!r}")


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------
def _seed(emu: Emulator):
    base, auth = emu.admin_base, _basic(emu.admin_user, emu.admin_password)

    # 1. Settings: permit cleartext IMAP/SMTP auth on the plaintext listeners, and pin
    #    `server.hostname` to the container's bridge IP. Stalwart builds the JMAP session
    #    `apiUrl`/`uploadUrl` as `http://<server.hostname>:8080/...` (the classic
    #    reverse-proxy "internal host leaks into .well-known/jmap" case — stalw.art
    #    /docs/server/reverse-proxy). The default is the container id, which is
    #    unresolvable from the host; the bridge IP is reachable on the native :8080, so
    #    the URL `JmapAdapter` follows off the session actually resolves.
    settings = [
        ["imap.auth.allow-plain-text", "true"],
        ["session.auth.mechanisms", "[plain, login]"],
        ["server.hostname", emu.container_ip],
        # Let an authenticated account send from its own address over the JMAP
        # `EmailSubmission` relay. Stalwart's default sender-authorization rejects
        # the MAIL-FROM ("501 5.5.4 You are not allowed to send from this address")
        # on the JMAP submission path even for the account's own identity address
        # (the SMTP-587 submission path the gmail/yahoo profiles use is unaffected),
        # which would otherwise fail the fastmail JMAP send round-trip.
        ["session.auth.must-match-sender", "false"],
    ]
    status, payload = _admin_call(base, auth, "POST", "/api/settings",
                                  [{"type": "insert", "prefix": None,
                                    "values": settings, "assert_empty": False}])
    assert status == 200, f"settings insert failed: {status} {payload}"
    status, payload = _admin_call(base, auth, "GET", "/api/reload")
    assert status == 200, f"settings reload failed: {status} {payload}"

    # 2. The local domain, then one individual per profile (roles=["user"] grants the
    #    IMAP/SMTP/JMAP permissions a fresh principal otherwise lacks).
    status, payload = _admin_call(base, auth, "POST", "/api/principal",
                                  {"type": "domain", "name": EMU_DOMAIN})
    assert status == 200, f"domain create failed: {status} {payload}"

    for stem, (provider, password) in _ACCOUNTS.items():
        address = f"{stem}@{EMU_DOMAIN}"
        status, payload = _admin_call(
            base, auth, "POST", "/api/principal",
            {"type": "individual", "name": address, "emails": [address],
             "secrets": [password], "roles": ["user"]})
        assert status == 200, f"{provider} create failed: {status} {payload}"
        emu.profiles[provider] = Profile(
            name=address, provider=provider, host=emu.host, address=address,
            password=password, imap_port=_MAPPED[PORT_IMAP],
            smtp_port=_MAPPED[PORT_SMTP], imaps_port=_MAPPED[PORT_IMAPS])

    # 3. fastmail: JMAP session URL + the Bearer token voa registers (base64 addr:pass —
    #    Stalwart accepts it as a Bearer credential at /.well-known/jmap).
    fm = emu.profiles["fastmail"]
    # Point the session resource at the SAME reachable host the advertised apiUrl uses
    # (the bridge IP on native :8080), so the session fetch and every follow-up JMAP call
    # resolve to one host.
    fm.jmap_url = f"http://{emu.container_ip}:{PORT_HTTP}/.well-known/jmap"
    fm.token = base64.b64encode(f"{fm.address}:{fm.password}".encode()).decode()
    # Stalwart 401s the FIRST Bearer request for a freshly-created account, then caches and
    # serves 200s. Prime that cache so the first real ``Bearer <token>`` request (ours, or
    # voa's JmapAdapter) succeeds deterministically.
    _warm_bearer(fm.jmap_url, fm.token)

    # 4. gmail: rename Drafts -> [Gmail]/Drafts (keeps \Drafts) and Sent Items -> Sent, then
    #    upload the per-account auto-file-Sent Sieve over ManageSieve.
    gm = emu.profiles["gmail"]
    conn = _imap_login(gm.host, gm.imap_port, gm.address, gm.password)
    try:
        _ok(conn.rename("Drafts", "[Gmail]/Drafts"), "rename Drafts")
        _ok(conn.rename('"Sent Items"', "Sent"), "rename Sent Items")
    finally:
        conn.logout()
    sieve = _ManageSieve(gm.host, _MAPPED[PORT_SIEVE])
    try:
        sieve.authenticate(gm.address, gm.password)
        sieve.put_and_activate("autosent", _GMAIL_SIEVE)
    finally:
        sieve.close()

    # 5. yahoo: a plain profile with STANDARD (real-Yahoo) folder names. Stalwart's
    #    default sent folder is the Exchange-style "Sent Items"; real Yahoo — and the
    #    generic IMAP path this profile emulates — names it "Sent". Rename it so the
    #    profile is faithful (DN Decision 2: "standard folder names") and the generic
    #    send path APPENDs the sent copy to a space-free \Sent mailbox.
    ya = emu.profiles["yahoo"]
    conn = _imap_login(ya.host, ya.imap_port, ya.address, ya.password)
    try:
        _ok(conn.rename('"Sent Items"', "Sent"), "rename yahoo Sent Items")
    finally:
        conn.logout()


def _warm_bearer(jmap_url, token, retries=10):
    """Prime Stalwart's per-account credential cache so ``Bearer <base64(addr:pass)>`` is
    accepted.

    A cold Bearer request for a freshly-created account 401s (Stalwart first tries to parse
    it as an OAuth access token). A SUCCESSFUL Basic request with the same base64 credential
    populates the credential cache, after which the identical blob is honoured as a Bearer
    token persistently — which is exactly the scheme ``JmapAdapter`` emits. So we prime with
    Basic, then confirm the Bearer path is live."""
    for _ in range(retries):
        basic = urllib.request.Request(jmap_url, headers={"Authorization": f"Basic {token}"})
        try:
            with urllib.request.urlopen(basic, timeout=15) as r:
                if r.status != 200:
                    time.sleep(0.5)
                    continue
        except (urllib.error.HTTPError, OSError):
            time.sleep(0.5)
            continue
        bearer = urllib.request.Request(
            jmap_url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(bearer, timeout=15) as r:
                if r.status == 200:
                    return
        except (urllib.error.HTTPError, OSError):
            pass
        time.sleep(0.5)


def _basic(user, password):
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def _ok(result, what):
    typ, _ = result
    assert typ == "OK", f"{what} failed: {result!r}"


# Filled in once the container is started so `_seed` can read the host-mapped ports.
_MAPPED: dict = {}


# ---------------------------------------------------------------------------
# The session-scoped fixture
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def stalwart_emulator():
    """Start one seeded Stalwart container, yield an :class:`Emulator`, tear it down.

    Auto-skips (secondary guard) when the ``[e2e]`` extra or a reachable Docker daemon is
    absent — the primary exclusion is ``addopts = -m "not e2e"``."""
    testcontainers = pytest.importorskip(
        "testcontainers.core.container",
        reason="the [e2e] extra (testcontainers) is not installed")
    DockerContainer = testcontainers.DockerContainer

    container = (DockerContainer(STALWART_IMAGE)
                 .with_exposed_ports(PORT_HTTP, PORT_IMAP, PORT_IMAPS,
                                     PORT_SMTP, PORT_SIEVE))
    try:
        container.start()
    except Exception as e:  # noqa: BLE001 — any docker-side failure means "skip locally"
        pytest.skip(f"Docker unavailable for the E2E emulator: {e!r}")

    try:
        host = container.get_container_host_ip()
        _MAPPED.clear()
        for internal in (PORT_HTTP, PORT_IMAP, PORT_IMAPS, PORT_SMTP, PORT_SIEVE):
            _MAPPED[internal] = int(container.get_exposed_port(internal))

        admin_password = _read_admin_password(container)
        emu = Emulator(host=host, http_port=_MAPPED[PORT_HTTP],
                       admin_user="admin", admin_password=admin_password,
                       container_ip=_container_ip(container))
        _wait_for_admin(emu.admin_base, _basic("admin", admin_password))
        _seed(emu)
        yield emu
    finally:
        container.stop()


def _container_ip(container) -> str:
    """The container's docker bridge IP (reachable from the host on native ports).

    Used as ``server.hostname`` so Stalwart advertises a JMAP ``apiUrl`` the host can
    actually reach (the mapped host port can never appear there — Stalwart always
    appends the internal listener port, :8080)."""
    wrapped = container.get_wrapped_container()
    wrapped.reload()
    net = wrapped.attrs["NetworkSettings"]
    ip = net.get("IPAddress")
    if not ip:
        ip = next(iter(net["Networks"].values()))["IPAddress"]
    return ip


def _read_admin_password(container, timeout=60):
    """The stock entrypoint prints ``…account is 'admin' with password 'XXXX'`` on first
    boot; parse it from the container logs (the password is randomised per boot)."""
    import re

    deadline = time.time() + timeout
    pattern = re.compile(r"administrator account is '([^']+)' with password '([^']+)'")
    while time.time() < deadline:
        stdout, stderr = container.get_logs()
        text = (stdout + stderr).decode(errors="replace")
        m = pattern.search(text)
        if m:
            return m.group(2)
        time.sleep(0.5)
    raise RuntimeError("could not read the Stalwart admin password from container logs")
