"""Profile sanity checks for the Dockerized-emulator E2E tier.

Proves the ``stalwart_emulator`` fixture (tests/e2e/conftest.py) seeds three usable
provider profiles, per DN-mail-e2e-emulator-testing.md Decision 2. This is the fixture's
self-test — the send/draft round-trip cases (Decision 5) are a separate deliverable. Every
test is ``@pytest.mark.e2e`` and so excluded from the default population by ``-m "not e2e"``.
"""
import base64
import imaplib
import smtplib
import time
import urllib.error
import urllib.request
import uuid
from email.message import EmailMessage

import pytest

pytestmark = pytest.mark.e2e


def _login(profile):
    conn = imaplib.IMAP4(profile.host, profile.imap_port)
    conn.authenticate(
        "PLAIN",
        lambda _c: f"\x00{profile.address}\x00{profile.password}".encode())
    return conn


# ---------------------------------------------------------------------------
# fastmail — JMAP
# ---------------------------------------------------------------------------
def test_fastmail_jmap_session_resource_responds(stalwart_emulator):
    """(a) The fastmail JMAP session resource responds at its URL, authenticating with the
    exact ``Bearer <token>`` scheme ``JmapAdapter`` emits."""
    fm = stalwart_emulator.profiles["fastmail"]
    assert fm.jmap_url.endswith("/.well-known/jmap")

    req = urllib.request.Request(
        fm.jmap_url, headers={"Authorization": f"Bearer {fm.token}"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        assert resp.status == 200
        session = __import__("json").loads(resp.read().decode())

    # A real JMAP session document: mail + submission capabilities + a primary account.
    assert "urn:ietf:params:jmap:mail" in session["capabilities"]
    assert "urn:ietf:params:jmap:submission" in session["capabilities"]
    assert session["primaryAccounts"], "session must advertise a primary account"


def test_fastmail_bearer_token_is_rejected_when_wrong(stalwart_emulator):
    """The JMAP endpoint genuinely authenticates (a bogus Bearer is refused, not waved
    through) — so the 200 above is real auth, not an open endpoint."""
    fm = stalwart_emulator.profiles["fastmail"]
    bogus = base64.b64encode(b"fastmail@emu.test:wrong").decode()
    req = urllib.request.Request(fm.jmap_url, headers={"Authorization": f"Bearer {bogus}"})
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=15)
    assert exc.value.code == 401


# ---------------------------------------------------------------------------
# gmail — IMAP special-use + Sieve auto-file-Sent
# ---------------------------------------------------------------------------
def test_gmail_profile_advertises_gmail_drafts_special_use(stalwart_emulator):
    """(b1) LIST ... RETURN (SPECIAL-USE) shows a ``[Gmail]/Drafts`` mailbox carrying the
    RFC 6154 ``\\Drafts`` attribute — the resolution path our IMAP adapter relies on."""
    gm = stalwart_emulator.profiles["gmail"]
    conn = _login(gm)
    try:
        typ, data = conn._simple_command(
            "LIST", '""', '"*"', "RETURN", "(SPECIAL-USE)")
        typ, lines = conn._untagged_response(typ, data, "LIST")
    finally:
        conn.logout()

    rendered = [ln.decode() if isinstance(ln, bytes) else str(ln) for ln in lines]
    drafts = [ln for ln in rendered if "[Gmail]/Drafts" in ln]
    assert drafts, f"no [Gmail]/Drafts mailbox advertised: {rendered}"
    assert "\\Drafts" in drafts[0], f"[Gmail]/Drafts lacks the \\Drafts attribute: {drafts[0]}"


def test_gmail_smtp_submission_is_auto_filed_into_sent(stalwart_emulator):
    """(b2) A message the gmail account submits over SMTP auto-appears in its Sent folder
    (server-side Sieve), so the send path need not APPEND to Sent itself."""
    gm = stalwart_emulator.profiles["gmail"]
    marker = uuid.uuid4().hex
    msg = EmailMessage()
    msg["From"] = gm.address
    msg["To"] = gm.address
    msg["Subject"] = f"auto-file-sent {marker}"
    msg.set_content("emulator sent-filing probe")

    smtp = smtplib.SMTP(gm.host, gm.smtp_port, timeout=15)
    try:
        smtp.ehlo()
        smtp.login(gm.address, gm.password)
        smtp.send_message(msg)
    finally:
        smtp.quit()

    assert _wait_for_marker(gm, "Sent", marker), \
        "the SMTP-submitted message never appeared in Sent"


# ---------------------------------------------------------------------------
# yahoo — plain IMAP/SMTP
# ---------------------------------------------------------------------------
def test_yahoo_profile_is_plain_imap_with_standard_folders(stalwart_emulator):
    """(c) yahoo is a plain RFC 3501 account: IMAP login works and it exposes standard
    folder names (no [Gmail]/* remapping)."""
    ya = stalwart_emulator.profiles["yahoo"]
    conn = _login(ya)
    try:
        typ, boxes = conn.list()
        assert typ == "OK"
        names = " ".join(b.decode() for b in boxes)
        assert "INBOX" in names
        assert "[Gmail]" not in names, f"yahoo must not carry a Gmail layout: {names}"
        assert conn.select("INBOX")[0] == "OK"
    finally:
        conn.logout()


def test_yahoo_smtp_submission_round_trips_to_inbox(stalwart_emulator):
    """yahoo has a working authenticated SMTP submission path (self-send lands in INBOX —
    no auto-file-Sent Sieve, unlike gmail)."""
    ya = stalwart_emulator.profiles["yahoo"]
    marker = uuid.uuid4().hex
    msg = EmailMessage()
    msg["From"] = ya.address
    msg["To"] = ya.address
    msg["Subject"] = f"yahoo probe {marker}"
    msg.set_content("plain profile probe")

    smtp = smtplib.SMTP(ya.host, ya.smtp_port, timeout=15)
    try:
        smtp.ehlo()
        smtp.login(ya.address, ya.password)
        smtp.send_message(msg)
    finally:
        smtp.quit()

    assert _wait_for_marker(ya, "INBOX", marker), \
        "the SMTP-submitted message never arrived in INBOX"


# ---------------------------------------------------------------------------
def _wait_for_marker(profile, mailbox, marker, timeout=20):
    """Poll ``mailbox`` until a message whose subject carries ``marker`` shows up."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        conn = _login(profile)
        try:
            conn.select(mailbox)
            typ, data = conn.search(None, "SUBJECT", marker)
            if typ == "OK" and data and data[0].split():
                return True
        finally:
            conn.logout()
        time.sleep(1)
    return False
