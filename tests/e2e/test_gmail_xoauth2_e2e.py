r"""E2E for the Gmail Workspace **XOAUTH2** read+send path — the real
``GmailXoauth2Adapter`` driven end-to-end against a live **Dovecot** emulator
(DN-mail-e2e-emulator-testing.md §Fidelity; task #71).

WHY DOVECOT (not Stalwart). Our capability matrix is split across two emulators, and
that split is the irreducible floor:

* **Stalwart** (the ``stalwart_emulator`` fixture) speaks JMAP + IMAP + SMTP and does
  PLAIN/LOGIN SASL — it covers the fastmail(JMAP)/gmail/yahoo(password) profiles. But it
  does **not** accept the XOAUTH2 SASL blob our adapter emits (``user=…\x01auth=Bearer
  …\x01\x01``); its bearer-SASL support is OAUTHBEARER-shaped. So Stalwart cannot drive
  the real Workspace-XOAUTH2 path.
* **Dovecot** (the ``dovecot_xoauth2_emulator`` fixture) DOES accept XOAUTH2 SASL against
  an RFC 7662 introspection endpoint — but speaks no JMAP. Neither server alone covers
  both JMAP and XOAUTH2, so two emulators is the minimum, not an accident.

WHAT THIS TEST PROVES (end-to-end, over the wire, against the running container):

* **IMAP XOAUTH2 login** — the real ``GmailXoauth2Adapter`` opens a REAL implicit-TLS
  (verification-off) IMAPS connection, authenticates with ``AUTHENTICATE XOAUTH2`` using
  the adapter's own unmodified ``_xoauth2_raw`` blob and a stubbed **zero-network** token
  provider, and issues a real ``LIST`` (``list_folders()``). Dovecot introspects the token
  against the RFC 7662 stub CONTAINER on the shared testcontainers network, gets
  ``active:true`` + the account email, and admits the session.
* **SMTP XOAUTH2 send** — ``create_draft`` APPENDs a draft to the ``\Drafts`` special-use
  mailbox, then ``send_draft`` opens the submission port, ``STARTTLS`` → ``EHLO`` →
  ``AUTH XOAUTH2`` (same ``_xoauth2_raw`` blob) → ``sendmail``. Dovecot relays the accepted
  submission to the SMTP-sink CONTAINER; the test asserts the message truly arrived there.

Both auxiliaries (the introspection stub and the SMTP sink) run as containers on the same
throwaway testcontainers network as Dovecot, NOT in-process on the host: a container here
cannot reach a host-gateway-published port, because this machine's host firewall drops
docker-bridge → host traffic (see the fixture's topology note in ``conftest.py``).

The adapter is driven UNMODIFIED — no ``conn_factory`` injection (it dials its own
``imaplib.IMAP4_SSL`` with a non-verifying context because ``tls_verify=False``), and the
real ``_xoauth2_raw`` SASL form is used on both channels. The only stub is the OAuth token
PROVIDER (a zero-argument callable returning a fixed string), because minting a real Google
access token needs a live OAuth token endpoint — orthogonal to what this tier validates.

Every test is ``@pytest.mark.e2e`` and so excluded from the default population by
``addopts = -m "not e2e"``.
"""
import imaplib
import time

import pytest

from vidushi_oa.mail.xoauth2 import GmailXoauth2Adapter

pytestmark = pytest.mark.e2e


def _adapter(emu, token_provider):
    """A real ``GmailXoauth2Adapter`` pointed at the Dovecot emulator's IMAPS +
    submission ports with verification off — no ``conn_factory`` injected, so the
    adapter dials its own ``imaplib.IMAP4_SSL`` through its own (non-verifying, because
    ``tls_verify=False``) ``_ssl_context``. ``access_token`` is the zero-network provider."""
    return GmailXoauth2Adapter(
        account="gmail-xoauth2-e2e", source_tag="G",
        host=emu.host, user=emu.address, access_token=token_provider,
        port=emu.imaps_port, smtp_host=emu.host, smtp_port=emu.smtp_port,
        tls_verify=False)


def _sink_received(sink_container, marker, timeout=25):
    """Poll the SMTP-sink container's logs until a relayed message line carrying
    ``marker`` appears; return the matching ``SINK-RECEIVED`` log line or ``None``.

    The sink prints one ``SINK-RECEIVED <message>`` line per accepted submission, so
    the marker in the message's ``Message-ID`` surfaces here once Dovecot has relayed
    the XOAUTH2-authenticated submission through to it."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        stdout, stderr = sink_container.get_logs()
        text = (stdout + stderr).decode(errors="replace")
        for line in text.splitlines():
            if line.startswith("SINK-RECEIVED") and marker in line:
                return line
        time.sleep(0.5)
    return None


def test_dovecot_imap_xoauth2_login_lists_folders(dovecot_xoauth2_emulator):
    """(a) IMAP XOAUTH2 login works: ``GmailXoauth2Adapter.list_folders()`` drives
    ``authenticate('XOAUTH2', …)`` over a REAL implicit-TLS IMAPS connection to the live
    Dovecot container, and the zero-network token provider is invoked exactly once."""
    emu = dovecot_xoauth2_emulator
    calls = {"n": 0}

    def token_provider():
        calls["n"] += 1
        return emu.token

    adapter = _adapter(emu, token_provider)
    folders = adapter.list_folders()

    # A real implicit-TLS IMAPS connection was opened and authenticated — not a fake,
    # not plaintext. The only auth path for this password-less adapter is XOAUTH2 SASL.
    assert isinstance(adapter._connection, imaplib.IMAP4_SSL)
    # The token came from the stubbed provider, minted once, with zero network I/O.
    assert calls["n"] == 1, f"token provider invoked {calls['n']}x (expected 1)"
    # LIST round-tripped: the account's INBOX is present among the listed mailboxes.
    assert any("INBOX" in f for f in folders), f"INBOX not listed: {folders}"


def test_dovecot_xoauth2_create_and_send_draft_relays_to_sink(dovecot_xoauth2_emulator):
    """(b) ``create_draft`` (IMAP APPEND to ``\\Drafts``) then ``send_draft``
    (SMTP STARTTLS → EHLO → ``AUTH XOAUTH2`` → ``sendmail``) against the live Dovecot
    container; the accepted submission is relayed to the SMTP-sink container."""
    emu = dovecot_xoauth2_emulator
    adapter = _adapter(emu, lambda: emu.token)

    marker = "dovecot-xoauth2-e2e-1@gmail.test"
    raw = (b"From: " + emu.address.encode() + b"\r\n"
           b"To: support@vendor.test\r\n"
           b"Subject: warranty claim\r\n"
           b"Message-ID: <" + marker.encode() + b">\r\n\r\n"
           b"Please find my invoice attached.\r\n")

    draft_id = adapter.create_draft(raw)
    assert draft_id, "create_draft returned no draft id"

    message_id = adapter.send_draft(draft_id)
    # send_draft returns the draft's own Message-ID once the submission is accepted.
    assert message_id == f"<{marker}>", message_id

    # The SMTP XOAUTH2 submission genuinely relayed through Dovecot to the sink.
    relayed = _sink_received(emu.sink, marker)
    assert relayed is not None, "the XOAUTH2 submission never reached the SMTP sink"
