"""E2E for the Gmail ``X-GM-RAW`` search path — the real ``GmailImapAdapter.search``
driven against the live Stalwart emulator (DN-mail-e2e-emulator-testing.md §Fidelity;
task #68).

WHAT THE EMULATOR ACTUALLY DOES (empirically established 2026-07-29 against
``stalwartlabs/mail-server:v0.11.8-alpine``, the ``gmail`` profile of the
``stalwart_emulator`` fixture):

* Its ``CAPABILITY`` list is a standard IMAP4rev2/rev1 set — it does **not** advertise
  ``X-GM-EXT-1``. ``X-GM-RAW`` / ``X-GM-THRID`` are Gmail-proprietary and Stalwart
  implements neither.
* ``a UID SEARCH X-GM-RAW "…"`` is **rejected with a tagged ``BAD``**: Stalwart parses the
  ``X-GM-RAW`` token as a message sequence-set and fails —
  ``Invalid sequence set "X-GM-RAW", found invalid character '88' at position 0.``
  ``imaplib`` surfaces that tagged ``BAD`` as ``imaplib.IMAP4.error``.
* A plain RFC 3501 ``UID SEARCH SUBJECT …`` on the same connection returns ``OK`` — the
  server is fully functional; only the Gmail extension is absent.

So a self-hosted server cannot reproduce Gmail's proprietary full-text search SEMANTICS.
This test therefore covers the MAX the emulator allows — the CLIENT PROTOCOL/round-trip
path — end-to-end against the running container:

WHAT THIS TEST PROVES
  * ``GmailImapAdapter`` opens a REAL (implicit-TLS, verification-off) IMAP connection to
    the live container, logs in and selects INBOX (the ``search`` call gets far enough to
    issue its SEARCH, so login+select succeeded over the wire).
  * It emits a correctly-formed, correctly-ESCAPED ``UID SEARCH X-GM-RAW "<query>"`` — the
    exact quoted-phrase escaping from the CR-OA-025 bug history — captured off the wire
    (the connection's ``send``) and asserted byte-for-byte, for a query carrying both
    embedded quotes and a backslash.
  * The command really round-trips: the server processes those exact bytes and its own
    response (a tagged ``BAD`` echoing the ``X-GM-RAW`` token) comes back and is surfaced
    by the adapter as ``imaplib.IMAP4.error`` — imaplib's structured protocol-error signal,
    not an unhandled Python-level crash (no ``AttributeError``/``TypeError`` from
    mis-parsing a response the adapter never expected).

WHAT THIS TEST DOES NOT PROVE
  * Gmail's ``X-GM-RAW`` full-text search SEMANTICS (which messages a real query matches)
    — that is real-Gmail-only and cannot be emulated. Surfacing the server's ``BAD`` is
    the *correct* adapter behaviour here: real Gmail never ``BAD``s a well-formed
    ``X-GM-RAW``, so propagating a non-Gmail server's rejection is by design, not a bug.

Every test is ``@pytest.mark.e2e`` and so excluded from the default population by
``addopts = -m "not e2e"``.
"""
import imaplib
from email.message import EmailMessage

import pytest

from vidushi_oa.mail.imap import GmailImapAdapter

pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# Seeding + wire-capture helpers
# ---------------------------------------------------------------------------
def _seed_inbox(profile, subject):
    """APPEND one message into the gmail profile's INBOX over plaintext IMAP (143), so the
    adapter's SEARCH runs against a populated, real mailbox."""
    conn = imaplib.IMAP4(profile.host, profile.imap_port)
    try:
        conn.authenticate(
            "PLAIN",
            lambda _c: f"\x00{profile.address}\x00{profile.password}".encode())
        msg = EmailMessage()
        msg["From"] = "seller@shop.example"
        msg["To"] = profile.address
        msg["Subject"] = subject
        msg.set_content("Thank you for registering. Warranty term is 24 months.")
        typ, _ = conn.append("INBOX", None, None, msg.as_bytes())
        assert typ == "OK", typ
    finally:
        conn.logout()


def _adapter(profile):
    """A real ``GmailImapAdapter`` pointed at the emulator's implicit-TLS IMAPS port with
    verification off — the object the ``endpoint`` override (``tls_verify: false``) builds
    for a Gmail account. No ``conn_factory`` is injected, so the adapter dials its own
    ``imaplib.IMAP4_SSL`` through its own (non-verifying) ``_ssl_context``."""
    return GmailImapAdapter(
        account="gmail-e2e", source_tag="gmail",
        host=profile.host, user=profile.address, password=profile.password,
        port=profile.imaps_port, tls_verify=False)


def _connect_and_spy_send(adapter):
    """Force the adapter to open its real connection (login + select INBOX over TLS), then
    wrap the live connection's ``send`` so every command line the adapter puts on the wire
    is captured. Returns the capture list."""
    conn = adapter._conn()
    sent = []
    original_send = conn.send

    def spy(data):
        sent.append(bytes(data))
        return original_send(data)

    conn.send = spy
    return sent


# The CR-OA-025 escaping contract, exercised end-to-end. Each case pairs a raw query with
# the EXACT quoted/escaped token the adapter must place after ``X-GM-RAW`` on the wire.
# The expected tokens are written out literally (NOT recomputed via the adapter's own
# ``.replace`` chain) so the assertion is an independent oracle, not a tautology.
_ESCAPE_CASES = [
    # embedded double-quotes -> each inner quote backslash-escaped, whole thing re-quoted.
    ('subject:"warranty registration"',
     b'"subject:\\"warranty registration\\""'),
    # a backslash (and a space) -> the backslash is doubled, no quotes to escape.
    ('rfc822msgid:a\\b plus',
     b'"rfc822msgid:a\\\\b plus"'),
]


@pytest.mark.parametrize("query,expected_token", _ESCAPE_CASES)
def test_gmail_xgmraw_command_is_escaped_and_round_trips(
        stalwart_emulator, query, expected_token):
    """``GmailImapAdapter.search`` emits the exact escaped ``UID SEARCH X-GM-RAW "…"`` on a
    real connection to the live emulator, and surfaces the server's actual response as
    ``imaplib.IMAP4.error`` (Stalwart rejects the Gmail-proprietary extension with a tagged
    ``BAD``) rather than an unhandled crash."""
    gm = stalwart_emulator.profiles["gmail"]
    _seed_inbox(gm, "Your warranty registration for AcmeWidget")

    # The adapter self-declares as the X-GM-RAW (raw_query) variant.
    adapter = _adapter(gm)
    assert "raw_query" in adapter.capabilities()

    sent = _connect_and_spy_send(adapter)
    # Real implicit-TLS IMAPS connection — not a fake and not plaintext.
    assert isinstance(adapter._connection, imaplib.IMAP4_SSL)

    # Stalwart implements no X-GM-RAW: it BADs the command, imaplib raises. This is the
    # server's real response round-tripping back — proof the bytes reached it.
    with pytest.raises(imaplib.IMAP4.error) as exc:
        adapter.search(query)

    # 1. The exact escaped wire form (the CR-OA-025 quoted-phrase-escaping path).
    xgmraw_lines = [line for line in sent if b"UID SEARCH X-GM-RAW " in line]
    assert xgmraw_lines, f"no X-GM-RAW command was sent; captured: {sent!r}"
    line = xgmraw_lines[0]
    assert b'UID SEARCH X-GM-RAW ' + expected_token in line, \
        f"wire form mismatch: {line!r} (expected token {expected_token!r})"

    # 2. The round-trip is genuine: the server echoed our exact command token in its BAD.
    assert "X-GM-RAW" in str(exc.value), \
        f"the BAD did not come from the server processing our command: {exc.value!r}"
