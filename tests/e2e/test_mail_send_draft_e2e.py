"""E2E send/draft round-trip smoke tests — the real ``voa`` verbs against a real
Stalwart emulator (DN-mail-e2e-emulator-testing.md §"Decision 5 — test specifications").

These drive the REAL ``voa mail-auth`` / ``mail-draft`` / ``mail-send`` verbs (through
``scripts/store.py``) against the Dockerized emulator seeded by the session-scoped
``stalwart_emulator`` fixture (tests/e2e/conftest.py), then verify the message truly
landed — over JMAP for the fastmail profile, over IMAP for the gmail/yahoo profiles.

The whole point is catching real-world-vs-mock divergence in the actual adapters (the
canonical round-1 empty-Fastmail-draft bug): the fakes-based suite proves the adapters
emit the right protocol against servers shaped like our fakes; this tier proves a real
send works.

ISOLATION (critical). Every verb runs in a subprocess whose env points the account
registry, the secret store, and the data dir at a per-test throwaway ``tmp_path`` —
``VIDUSHI_MAIL_CONFIG`` / ``VIDUSHI_SECRETS_FILE`` / ``VIDUSHI_SECRET_BACKEND=file`` /
``VIDUSHI_DATA_DIR`` / ``VIDUSHI_BACKEND=sqlite`` — so the tests NEVER read or mutate the
user's real fastmail/gmail/yahoo accounts, secrets, or store. Each emulator profile is
registered send-capable and pointed at the emulator purely via the #63 endpoint override.

Every test is ``@pytest.mark.e2e`` and so excluded from the default population by
``addopts = -m "not e2e"``.
"""
import base64
import imaplib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

import pytest

pytestmark = pytest.mark.e2e

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
STORE = os.path.join(ROOT, "scripts", "store.py")


# ---------------------------------------------------------------------------
# Isolated-env voa invocation
# ---------------------------------------------------------------------------
def _voa_env(tmp_path, fmt="json"):
    """A process env that isolates the account registry, secret store and data dir
    into ``tmp_path`` so nothing touches the user's real accounts/keyring/store."""
    env = dict(os.environ)
    env["VIDUSHI_MAIL_CONFIG"] = str(tmp_path / "accounts.json")
    env["VIDUSHI_SECRETS_FILE"] = str(tmp_path / "secrets.json")
    env["VIDUSHI_SECRET_BACKEND"] = "file"
    env["VIDUSHI_DATA_DIR"] = str(tmp_path / "data")
    env["VIDUSHI_BACKEND"] = "sqlite"
    env["VIDUSHI_SQLITE_PATH"] = str(tmp_path / "oa.db")
    env["VIDUSHI_FORMAT"] = fmt
    env.pop("PYTHON_KEYRING_BACKEND", None)
    env.pop("VIDUSHI_MAIL_ENDPOINTS", None)
    return env


def _voa(env, *args, stdin=None):
    """Run one ``voa`` verb via the ``scripts/store.py`` shim; return the completed
    process (caller asserts on returncode/stdout)."""
    return subprocess.run(
        [sys.executable, STORE, *args],
        capture_output=True, text=True, env=env, input=stdin, timeout=120)


def _account_name(profile):
    return f"{profile.provider}:{profile.address}"


def _register(env, profile):
    """Register ``profile`` as a send-capable account pointed at the emulator via the
    endpoint override. The secret is the JMAP Bearer token (fastmail) or the
    IMAP/SMTP password (gmail/yahoo), entered over stdin (never argv)."""
    secret = profile.token if profile.provider == "fastmail" else profile.password
    r = _voa(env, "mail-auth", "--provider", profile.provider,
             "--address", profile.address, "--send",
             "--endpoint", json.dumps(profile.endpoint()), stdin=secret + "\n")
    assert r.returncode == 0, f"mail-auth failed: {r.stdout!r} {r.stderr!r}"
    return _account_name(profile)


def _draft(env, account, address, subject, body):
    """Run ``voa mail-draft`` (self-send, ``--force`` past the verified-recipient
    guard) and return ``(draft_id, parsed_status)``."""
    r = _voa(env, "mail-draft", "--account", account, "--from", address,
             "--to", address, "--subject", subject, "--body", body, "--force")
    assert r.returncode == 0, f"mail-draft failed: {r.stdout!r} {r.stderr!r}"
    status = json.loads(r.stdout)
    assert status["status"] == "drafted", status
    return status["draft"], status


def _send(env, account, draft_id):
    """Run ``voa mail-send`` and return ``(message_id, parsed_status)``."""
    r = _voa(env, "mail-send", "--account", account, "--draft", draft_id)
    assert r.returncode == 0, f"mail-send failed: {r.stdout!r} {r.stderr!r}"
    status = json.loads(r.stdout)
    assert status["status"] == "sent", status
    return status["message_id"], status


# ---------------------------------------------------------------------------
# JMAP assertion helpers (fastmail profile) — plain HTTP + Bearer, same scheme
# JmapAdapter emits.
# ---------------------------------------------------------------------------
def _jmap_post(url, token, body):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def _jmap_session(profile):
    req = urllib.request.Request(
        profile.jmap_url, headers={"Authorization": f"Bearer {profile.token}"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        doc = json.loads(resp.read().decode())
    account_id = doc["primaryAccounts"]["urn:ietf:params:jmap:mail"]
    return doc["apiUrl"], account_id


def _jmap_mailbox_id(profile, api_url, account_id, role):
    payload = _jmap_post(api_url, profile.token, {
        "using": ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:mail"],
        "methodCalls": [["Mailbox/query",
                         {"accountId": account_id, "filter": {"role": role}}, "0"]]})
    ids = payload["methodResponses"][0][1].get("ids") or []
    return ids[0] if ids else ""


def _jmap_email(profile, api_url, account_id, email_id):
    payload = _jmap_post(api_url, profile.token, {
        "using": ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:mail"],
        "methodCalls": [["Email/get",
                         {"accountId": account_id, "ids": [email_id],
                          "properties": ["subject", "from", "to", "keywords",
                                         "mailboxIds", "textBody", "bodyValues"],
                          "fetchTextBodyValues": True}, "0"]]})
    lst = payload["methodResponses"][0][1].get("list") or []
    return lst[0] if lst else None


# ---------------------------------------------------------------------------
# IMAP assertion helpers (gmail/yahoo profiles)
# ---------------------------------------------------------------------------
def _imap_login(profile):
    conn = imaplib.IMAP4(profile.host, profile.imap_port)
    conn.authenticate(
        "PLAIN", lambda _c: f"\x00{profile.address}\x00{profile.password}".encode())
    return conn


def _special_use_mailbox(profile, attr):
    """The mailbox name carrying the RFC 6154 special-use ``attr`` (e.g. ``\\Drafts``,
    ``\\Sent``) — resolved over plaintext IMAP so an assertion targets the SAME folder
    the adapter resolved, regardless of its literal name (``[Gmail]/Drafts``, ``Sent
    Items``, …)."""
    conn = _imap_login(profile)
    try:
        typ, data = conn._simple_command("LIST", '""', '"*"', "RETURN", "(SPECIAL-USE)")
        typ, lines = conn._untagged_response(typ, data, "LIST")
    finally:
        conn.logout()
    needle = attr.lower().encode()
    for line in lines:
        raw = line if isinstance(line, bytes) else str(line).encode()
        if needle in raw.lower():
            name = raw.rsplit(b'"/"', 1)[-1].strip().strip(b'"')
            return name.decode(errors="replace")
    raise AssertionError(f"no mailbox advertises {attr} for {profile.address}: {lines}")


def _imap_subjects(profile, mailbox):
    """Every message in ``mailbox`` for ``profile`` as ``(uid, subject, meta)`` where
    ``meta`` carries the FLAGS (fresh login).

    A UID FETCH for ``(FLAGS BODY.PEEK[...])`` returns the header literal in a tuple and
    the FLAGS in a SEPARATE trailing bytes fragment, so ``meta`` is built by joining
    every bytes fragment of the per-message response — the FLAGS token lands there."""
    conn = _imap_login(profile)
    try:
        # Quote the mailbox — names carry spaces ("Sent Items") the IMAP grammar
        # rejects bare.
        typ, _ = conn.select(f'"{mailbox}"')
        if typ != "OK":
            return []
        typ, data = conn.uid("SEARCH", None, "ALL")
        uids = data[0].split() if (typ == "OK" and data and data[0]) else []
        subjects = []
        for uid in uids:
            typ, msg = conn.uid("FETCH", uid, "(FLAGS BODY.PEEK[HEADER.FIELDS (SUBJECT)])")
            subject, meta = "", b""
            for item in msg or []:
                if isinstance(item, tuple):
                    meta += b" " + (item[0] or b"")
                    subject = (item[1] or b"").decode(errors="replace")
                elif isinstance(item, bytes):
                    meta += b" " + item
            subjects.append((uid.decode(), subject, meta.decode(errors="replace")))
        return subjects
    finally:
        conn.logout()


def _imap_wait(profile, mailbox, marker, timeout=25):
    """Poll ``mailbox`` until a Subject carries ``marker``; return the matching
    ``(uid, header, descriptor)`` tuple or ``None``."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for row in _imap_subjects(profile, mailbox):
            if marker in row[1]:
                return row
        time.sleep(1)
    return None


def _unique(prefix):
    return f"{prefix} {base64.b32encode(os.urandom(6)).decode().rstrip('=')}"


# ---------------------------------------------------------------------------
# Case 1 — fastmail / JMAP: blob upload + Email/import draft, EmailSubmission send
# ---------------------------------------------------------------------------
def test_fastmail_jmap_draft_round_trip(stalwart_emulator, tmp_path):
    """`voa mail-draft` -> the draft REALLY lands in the emulator Drafts via blob upload
    + `Email/import`: fetch it back over JMAP and compare From/To/Subject/body + `$draft`."""
    fm = stalwart_emulator.profiles["fastmail"]
    env = _voa_env(tmp_path)
    account = _register(env, fm)

    subject = _unique("JMAP round-trip")
    body = "Please treat this as a warranty claim draft. " + _unique("body")
    draft_id, _ = _draft(env, account, fm.address, subject, body)

    api_url, account_id = _jmap_session(fm)
    drafts_id = _jmap_mailbox_id(fm, api_url, account_id, "drafts")
    assert drafts_id

    email = _jmap_email(fm, api_url, account_id, draft_id)
    assert email is not None, "the drafted email was not created on the server"
    assert email["subject"] == subject
    assert email["from"][0]["email"] == fm.address
    assert email["to"][0]["email"] == fm.address
    assert email["keywords"].get("$draft") is True, email["keywords"]
    assert email["mailboxIds"].get(drafts_id) is True, email["mailboxIds"]
    text = "".join(v["value"] for v in email["bodyValues"].values())
    assert body in text, text


@pytest.mark.xfail(strict=True, reason=(
    "REAL adapter bug this tier exists to catch (round-1-class divergence): "
    "vidushi_oa/mail/jmap.py JmapAdapter.send_draft builds EmailSubmission/set with only "
    "{emailId} and never resolves an Identity, but RFC 8621 §7 makes `identityId` a "
    "required property of EmailSubmission. A spec-compliant server (Stalwart) rejects the "
    "submission: invalidProperties [emailId, identityId] 'emailId and identityId "
    "properties are required.'. It only works against Fastmail, which leniently assigns a "
    "default identity. FIX: Identity/get the account's identity for the From address and "
    "include identityId in the create; then remove this xfail (strict -> XPASS flags it)."))
def test_fastmail_jmap_send_round_trip_moves_to_sent(stalwart_emulator, tmp_path):
    """`voa mail-send` -> EmailSubmission + onSuccessUpdateEmail moves the message to Sent
    and clears $draft/Drafts. Currently xfails on the missing-identityId adapter bug."""
    fm = stalwart_emulator.profiles["fastmail"]
    env = _voa_env(tmp_path)
    account = _register(env, fm)

    subject = _unique("JMAP send")
    draft_id, _ = _draft(env, account, fm.address, subject, "send round-trip body")

    api_url, account_id = _jmap_session(fm)
    drafts_id = _jmap_mailbox_id(fm, api_url, account_id, "drafts")
    sent_id = _jmap_mailbox_id(fm, api_url, account_id, "sent")
    assert drafts_id and sent_id

    message_id, _ = _send(env, account, draft_id)
    assert message_id, "mail-send returned no message id"

    after = _jmap_email(fm, api_url, account_id, draft_id)
    assert after is not None
    assert after["mailboxIds"].get(sent_id) is True, \
        f"sent message not filed in Sent: {after['mailboxIds']}"
    assert drafts_id not in after["mailboxIds"], \
        f"sent message still in Drafts: {after['mailboxIds']}"
    assert "$draft" not in after["keywords"], \
        f"$draft keyword not cleared: {after['keywords']}"


# ---------------------------------------------------------------------------
# Case 2 — gmail / IMAP+SMTP: APPEND to [Gmail]/Drafts (special-use), SMTP send,
# server-side Sieve files it to Sent, voa SKIPS the Sent APPEND.
# ---------------------------------------------------------------------------
def test_gmail_imap_draft_appends_to_special_use_drafts_then_sends_to_sent(
        stalwart_emulator, tmp_path):
    gm = stalwart_emulator.profiles["gmail"]
    env = _voa_env(tmp_path)
    account = _register(env, gm)

    drafts_box = _special_use_mailbox(gm, "\\Drafts")
    sent_box = _special_use_mailbox(gm, "\\Sent")
    assert drafts_box == "[Gmail]/Drafts", drafts_box  # the Gmail-shaped layout

    subject = _unique("Gmail round-trip")
    body = "Return/RMA request draft. " + _unique("body")
    draft_id, _ = _draft(env, account, gm.address, subject, body)

    # The draft resolved the \Drafts special-use mailbox ([Gmail]/Drafts) and APPENDed
    # there — assert it is present, flagged \Draft.
    drafted = _imap_wait(gm, drafts_box, subject)
    assert drafted is not None, f"draft never landed in {drafts_box}"
    assert "\\Draft" in drafted[2], f"draft not flagged \\Draft: {drafted}"

    message_id, _ = _send(env, account, draft_id)
    assert message_id

    # Gmail-shaped: voa skips the Sent APPEND; the profile's Sieve auto-files the
    # SMTP submission into Sent. Assert it is in Sent...
    sent = _imap_wait(gm, sent_box, subject)
    assert sent is not None, "sent message never auto-filed into Sent"
    # ...and is NOT stranded flagged \Draft in the Drafts mailbox.
    leftover = [r for r in _imap_subjects(gm, drafts_box)
                if subject in r[1] and "\\Draft" in r[2]]
    assert not leftover, f"draft left stranded flagged \\Draft: {leftover}"


# ---------------------------------------------------------------------------
# Case 3 — yahoo / IMAP+SMTP (generic): APPEND to Drafts, SMTP send, voa APPENDs to Sent.
# ---------------------------------------------------------------------------
def test_yahoo_imap_draft_then_send_appends_to_sent(stalwart_emulator, tmp_path):
    ya = stalwart_emulator.profiles["yahoo"]
    env = _voa_env(tmp_path)
    account = _register(env, ya)

    drafts_box = _special_use_mailbox(ya, "\\Drafts")
    sent_box = _special_use_mailbox(ya, "\\Sent")

    subject = _unique("Yahoo round-trip")
    body = "Generic IMAP draft. " + _unique("body")
    draft_id, _ = _draft(env, account, ya.address, subject, body)

    drafted = _imap_wait(ya, drafts_box, subject)
    assert drafted is not None, f"draft never landed in {drafts_box}"
    assert "\\Draft" in drafted[2], f"draft not flagged \\Draft: {drafted}"

    message_id, _ = _send(env, account, draft_id)
    assert message_id

    # Generic provider: voa itself APPENDs the sent copy to Sent.
    sent = _imap_wait(ya, sent_box, subject)
    assert sent is not None, f"sent message was not APPENDed to {sent_box}"


# ---------------------------------------------------------------------------
# Case 4 — AXI #9 end-to-end: mail-draft's TOON output carries a runnable
# mail-send --account ... --draft ... in next[].
# ---------------------------------------------------------------------------
def test_mail_draft_toon_next_hint_is_runnable_mail_send(stalwart_emulator, tmp_path):
    fm = stalwart_emulator.profiles["fastmail"]
    env = _voa_env(tmp_path, fmt="toon")
    account = _register(env, fm)

    r = _voa(env, "mail-draft", "--account", account, "--from", fm.address,
             "--to", fm.address, "--subject", _unique("AXI9"),
             "--body", "next-hint probe", "--force")
    assert r.returncode == 0, f"mail-draft failed: {r.stdout!r} {r.stderr!r}"

    # The TOON status carries the runnable confirm-and-send step for THIS draft.
    m = re.search(rf"mail-send --account {re.escape(account)} --draft (\S+)", r.stdout)
    assert m, f"no runnable mail-send next[] hint in TOON output:\n{r.stdout}"
    assert m.group(1), "the next[] mail-send hint carries no draft id"
