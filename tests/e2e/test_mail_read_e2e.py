"""E2E READ-path smoke tests — the real ``voa mail-search`` / ``mail-get`` verbs against
the Stalwart JMAP (``fastmail``) emulator profile (DN-mail-e2e-emulator-testing.md).

The existing E2E tier (``test_mail_send_draft_e2e.py``) only exercised the SEND/DRAFT
path; the READ path (``mail-search`` / ``mail-get``) had never been driven end-to-end.
This module closes that gap — and in doing so reproduces a class of read-path bugs the
fakes-based suite (``tests/test_cr_oa_020_jmap.py``) structurally cannot catch.

HEADLINE FINDING — the read path is broken against a spec-compliant JMAP server:

``JmapAdapter`` requests ``deliveredTo`` in its ``Email/get`` property projection
(``_EMAIL_PROPERTIES``, added in CR-OA-020 for the masked-alias trick). ``deliveredTo``
is NOT an RFC 8621 Email property, so a compliant server (Stalwart — and, per the field
report, real Fastmail) rejects it: the batched ``Email/query`` SUCCEEDS and returns the
matching ids, but the paired ``Email/get`` comes back as
``["error", {"type": "invalidArguments", "description": "Invalid property deliveredTo"}, "1"]``
inside an HTTP 200. ``JmapAdapter._parse()`` scans ``methodResponses`` only for an
``Email/get`` *response* and silently returns ``[]`` when it finds none — so the error is
SWALLOWED and every search / fetch collapses to a legitimate-looking empty result
(exit 0, ``count: 0``). The fake transport returns canned ``Email/get`` payloads that
already *contain* ``deliveredTo``, so it proves the adapter can PARSE the property but
never that a real server will accept it in a REQUEST — the exact mock-vs-real gap this
tier exists to catch.

VERDICT (per the triaged read-path bugs): 5a / 5b / 7 REPRODUCE on Stalwart → OUR CODE
BUG (the ``deliveredTo`` invalid-property + the ``_parse`` swallow), not Stalwart-vs-real
Fastmail drift. The ``{"text": query}`` filter itself is CORRECT and matches on Stalwart
(``test_text_filter_matches_on_the_compliant_jmap_server``). Bug 6 (the advertised
portable/compound grammar) has a distinct mechanism: there is no query-translation layer
at all, so ``subject:`` / ``newer_than:`` qualifiers are passed verbatim into the JMAP
``text`` filter and are silently ineffective (a no-op on the date bound; zero hits for a
bare qualifier). See ``test_portable_query_qualifiers_are_not_translated_for_jmap``.

The behavioural contracts the read path OWES (``mail-search`` returns the seeded rows;
``mail-get`` opens a message) are encoded as ``xfail(strict=True)`` — they fail today
because of the swallow, and will flip to XPASS (a hard failure) the moment the adapter is
fixed, forcing the markers to be removed. Nothing here patches production code.

ISOLATION mirrors ``test_mail_send_draft_e2e.py``: every ``voa`` verb runs in a subprocess
whose registry/secret-store/data-dir are pinned into a throwaway ``tmp_path`` and pointed
at the emulator purely via the #63 endpoint override, so the user's real accounts/store
are never touched. Seeding lands messages straight in the ``fastmail`` mailbox over JMAP
``Email/import`` (full control of ``receivedAt`` + an attachment) plus one real SMTP
send→fetch round-trip. Every test is ``@pytest.mark.e2e`` (excluded by ``-m 'not e2e'``).
"""
import base64
import datetime
import email.message
import email.utils
import json
import os
import smtplib
import subprocess
import sys
import time
import urllib.request

import pytest

from vidushi_oa.mail.jmap import _EMAIL_PROPERTIES

pytestmark = pytest.mark.e2e

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
STORE = os.path.join(ROOT, "scripts", "store.py")

CORE = "urn:ietf:params:jmap:core"
MAIL = "urn:ietf:params:jmap:mail"


# ---------------------------------------------------------------------------
# Isolated-env voa invocation (identical seam to test_mail_send_draft_e2e.py)
# ---------------------------------------------------------------------------
def _voa_env(tmp_path, fmt="json"):
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
    return subprocess.run(
        [sys.executable, STORE, *args],
        capture_output=True, text=True, env=env, input=stdin, timeout=120)


def _account_name(profile):
    return f"{profile.provider}:{profile.address}"


def _register_read(env, profile):
    """Register ``profile`` read-only (no ``--send``) pointed at the emulator via the
    endpoint override; the JMAP Bearer token is entered over stdin, never argv."""
    r = _voa(env, "mail-auth", "--provider", profile.provider,
             "--address", profile.address,
             "--endpoint", json.dumps(profile.endpoint()), stdin=profile.token + "\n")
    assert r.returncode == 0, f"mail-auth failed: {r.stdout!r} {r.stderr!r}"
    return _account_name(profile)


# ---------------------------------------------------------------------------
# Raw JMAP helpers — plain HTTP + Bearer, the same scheme JmapAdapter emits. These
# are the "ground truth" the voa verbs are compared against.
# ---------------------------------------------------------------------------
def _jmap_get(url, token):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def _jmap_post(url, token, body):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def _session(profile):
    doc = _jmap_get(profile.jmap_url, profile.token)
    account_id = doc["primaryAccounts"][MAIL]
    upload = (doc.get("uploadUrl") or "").replace("{accountId}", account_id)
    return doc["apiUrl"], account_id, upload


def _mailbox_id(api, token, account_id, role):
    payload = _jmap_post(api, token, {
        "using": [CORE, MAIL],
        "methodCalls": [["Mailbox/query",
                         {"accountId": account_id, "filter": {"role": role}}, "0"]]})
    ids = payload["methodResponses"][0][1].get("ids") or []
    return ids[0] if ids else ""


def _upload_blob(upload_url, token, raw):
    req = urllib.request.Request(
        upload_url, data=raw, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "message/rfc822"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())["blobId"]


def _query_ids(api, token, account_id, jmap_filter):
    """Raw ``Email/query`` ids for ``jmap_filter`` (no ``Email/get`` — so this NEVER
    trips the ``deliveredTo`` rejection; it is the clean ground-truth of what matched)."""
    payload = _jmap_post(api, token, {
        "using": [CORE, MAIL],
        "methodCalls": [["Email/query",
                         {"accountId": account_id, "filter": jmap_filter}, "0"]]})
    resp = payload["methodResponses"][0]
    assert resp[0] == "Email/query", f"unexpected raw query response: {payload}"
    return resp[1].get("ids") or []


def _rfc822(subject, from_addr, to_addr, body, received=None, attach=False):
    msg = email.message.EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Message-ID"] = email.utils.make_msgid(domain="emumail.org")
    if received is not None:
        msg["Date"] = email.utils.format_datetime(received)
    msg.set_content(body)
    if attach:
        msg.add_attachment(b"%PDF-1.4 fake invoice bytes", maintype="application",
                           subtype="pdf", filename="invoice.pdf")
    return msg.as_bytes()


def _import(api, token, account_id, inbox_id, blob_id, received):
    body = {"using": [CORE, MAIL], "methodCalls": [
        ["Email/import", {"accountId": account_id, "emails": {"e": {
            "blobId": blob_id, "mailboxIds": {inbox_id: True},
            "receivedAt": received.strftime("%Y-%m-%dT%H:%M:%SZ")}}}, "0"]]}
    payload = _jmap_post(api, token, body)
    created = payload["methodResponses"][0][1].get("created") or {}
    assert created, f"Email/import failed: {payload['methodResponses']}"
    return next(iter(created.values()))["id"]


# ---------------------------------------------------------------------------
# Module-scoped seed: land a spread of messages in the fastmail mailbox once.
# ---------------------------------------------------------------------------
class _Seed:
    def __init__(self, profile, api, account_id, inbox_id, messages, smtp_marker,
                 smtp_delivered):
        self.profile = profile
        self.api = api
        self.account_id = account_id
        self.inbox_id = inbox_id
        self.messages = messages            # list of dicts: subject/sender/jmap_id/...
        self.smtp_marker = smtp_marker
        self.smtp_delivered = smtp_delivered


@pytest.fixture(scope="module")
def seeded_fastmail(stalwart_emulator):
    """Seed the ``fastmail`` JMAP mailbox with a varied message set (recent + old,
    two senders, a shared ``Amazon`` keyword, one with a PDF attachment) plus one
    real SMTP send from the ``yahoo`` profile — a genuine send→fetch round-trip.

    Presence is confirmed here over RAW ``Email/query`` (bypassing the broken adapter)
    so every test starts from a mailbox proven non-empty."""
    fm = stalwart_emulator.profiles["fastmail"]
    ya = stalwart_emulator.profiles["yahoo"]
    api, account_id, upload = _session(fm)
    inbox_id = _mailbox_id(api, fm.token, account_id, "inbox")
    assert inbox_id, "fastmail account advertises no inbox mailbox"

    now = datetime.datetime.now(datetime.timezone.utc)
    plan = [
        # subject, sender, body, age(days), attachment
        ("Your Amazon.in order has shipped", "orders@amazon.in",
         "Amazon shipment tracking AB123 out for delivery", 1, False),
        ("Amazon invoice for your recent order", "invoice@amazon.in",
         "Amazon tax invoice attached for your records", 40, True),
        ("Flipkart delivery update", "noreply@flipkart.com",
         "Your Flipkart parcel is out for delivery today", 2, False),
        ("Netflix payment receipt", "info@netflix.com",
         "Your Netflix subscription renewed for the month", 10, False),
    ]
    messages = []
    for subject, sender, body, age, attach in plan:
        received = now - datetime.timedelta(days=age)
        raw = _rfc822(subject, sender, fm.address, body, received=received, attach=attach)
        blob = _upload_blob(upload, fm.token, raw)
        jmap_id = _import(api, fm.token, account_id, inbox_id, blob, received)
        messages.append({"subject": subject, "sender": sender, "jmap_id": jmap_id,
                         "age_days": age, "attach": attach})

    # One real SMTP send yahoo -> fastmail (same Stalwart, local delivery), then poll
    # RAW JMAP until it lands: this proves the send→fetch round-trip DELIVERS, so any
    # later inability of `voa mail-search` to find it is a read-path bug, not a lost mail.
    marker = "RoundTrip-" + base64.b32encode(os.urandom(6)).decode().rstrip("=")
    smtp_msg = email.message.EmailMessage()
    smtp_msg["Subject"] = marker + " delivery notice"
    smtp_msg["From"] = ya.address
    smtp_msg["To"] = fm.address
    smtp_msg["Message-ID"] = email.utils.make_msgid(domain="emumail.org")
    smtp_msg.set_content("round-trip body " + marker)
    with smtplib.SMTP(ya.host, ya.smtp_port, timeout=20) as smtp:
        smtp.starttls()
        smtp.login(ya.address, ya.password)
        smtp.send_message(smtp_msg)

    delivered = False
    deadline = time.time() + 30
    while time.time() < deadline:
        if _query_ids(api, fm.token, account_id, {"text": marker}):
            delivered = True
            break
        time.sleep(1)

    return _Seed(fm, api, account_id, inbox_id, messages, marker, delivered)


# ===========================================================================
# GROUND-TRUTH / POSITIVE CONTROLS — the emulator, the seed, and the JMAP `text`
# filter are healthy. These isolate the bug to our property projection.
# ===========================================================================
def test_seed_is_present_over_raw_jmap(seeded_fastmail):
    """Every seeded message + the SMTP round-trip is really in the mailbox (raw
    ``Email/query {inMailbox}`` count >= 5). Baseline that the mailbox is non-empty."""
    ids = _query_ids(seeded_fastmail.api, seeded_fastmail.profile.token,
                     seeded_fastmail.account_id,
                     {"inMailbox": seeded_fastmail.inbox_id})
    assert len(ids) >= len(seeded_fastmail.messages) + 1, ids
    assert seeded_fastmail.smtp_delivered, \
        "the SMTP send never delivered to the fastmail mailbox (round-trip broken)"


def test_text_filter_matches_on_the_compliant_jmap_server(seeded_fastmail):
    """Bug 5b evidence: the ``{"text": query}`` filter our adapter emits is CORRECT
    against a compliant JMAP server — ``Amazon`` matches the two Amazon messages,
    ``Flipkart`` matches one, and an absent keyword matches none. So a 0-count read is
    NEVER the filter's fault; it is the ``deliveredTo`` swallow downstream (5a)."""
    tok, api, acct = (seeded_fastmail.profile.token, seeded_fastmail.api,
                      seeded_fastmail.account_id)
    assert len(_query_ids(api, tok, acct, {"text": "Amazon"})) == 2
    assert len(_query_ids(api, tok, acct, {"text": "Flipkart"})) == 1
    assert _query_ids(api, tok, acct, {"text": "ZZZ-absent-keyword-xyzzy"}) == []


def test_deliveredTo_property_is_rejected_by_the_compliant_jmap_server(seeded_fastmail):
    """ROOT CAUSE (5a/7): the EXACT batched body ``JmapAdapter.search()`` sends — an
    ``Email/query {text}`` back-referenced into an ``Email/get`` requesting the real
    ``_EMAIL_PROPERTIES`` — has ``Email/query`` SUCCEED (2 ids) while the paired
    ``Email/get`` FAILS with ``invalidArguments`` naming ``deliveredTo``, all inside one
    HTTP 200. This is the server-side fact that the adapter's ``_parse`` then swallows."""
    assert "deliveredTo" in _EMAIL_PROPERTIES, (
        "guard tripped: this test pins the deliveredTo-rejection bug; if the property "
        "was removed from _EMAIL_PROPERTIES the read-path fix has landed — delete this "
        "test and the xfail markers below")
    tok, api, acct = (seeded_fastmail.profile.token, seeded_fastmail.api,
                      seeded_fastmail.account_id)
    payload = _jmap_post(api, tok, {
        "using": [CORE, MAIL],
        "methodCalls": [
            ["Email/query", {"accountId": acct, "filter": {"text": "Amazon"}}, "0"],
            ["Email/get", {"accountId": acct,
                           "#ids": {"resultOf": "0", "name": "Email/query", "path": "/ids"},
                           "properties": list(_EMAIL_PROPERTIES)}, "1"]]})
    responses = payload["methodResponses"]
    by_call = {r[2]: r for r in responses}
    assert by_call["0"][0] == "Email/query" and len(by_call["0"][1]["ids"]) == 2, responses
    assert by_call["1"][0] == "error", f"Email/get did not error: {responses}"
    assert by_call["1"][1]["type"] == "invalidArguments", by_call["1"]
    assert "deliveredTo" in by_call["1"][1].get("description", ""), by_call["1"]


def test_search_swallows_the_email_get_error_to_empty(seeded_fastmail):
    """Bug 5a end-to-end: the PRODUCTION ``JmapAdapter.search()`` returns ``[]`` even
    though ``Email/query`` matched — the ``Email/get`` ``invalidArguments`` error is
    swallowed by ``_parse`` (which only scans for an ``Email/get`` *response*). No
    exception, no surfaced error: a real failure masquerading as an empty mailbox."""
    from vidushi_oa.mail.jmap import JmapAdapter
    adapter = JmapAdapter(
        _account_name(seeded_fastmail.profile), "[FM]", seeded_fastmail.profile.token,
        session_url=seeded_fastmail.profile.jmap_url)
    # Ground truth: 2 match. The adapter swallows the paired Email/get error to nothing.
    ground_truth = _query_ids(seeded_fastmail.api, seeded_fastmail.profile.token,
                              seeded_fastmail.account_id, {"text": "Amazon"})
    assert len(ground_truth) == 2, ground_truth
    assert adapter.search("Amazon") == [], \
        "search no longer swallows the Email/get error — the deliveredTo fix has landed"


def test_forced_query_error_is_also_swallowed_to_empty(seeded_fastmail):
    """Bug 5a, forced-error arm: an INVALID filter makes ``Email/query`` ITSELF error
    (``unsupportedFilter``) at HTTP 200. ``search()``'s ``_parse`` looks only for an
    ``Email/get`` response, so this too collapses to ``[]`` — a server/filter error is
    indistinguishable from a legitimately empty result at the voa surface. (Driven with
    an injected transport because ``search()`` only ever emits a valid ``{text}``
    filter; the swallow lives in ``_parse``, which this exercises on a real server's
    error payload.)"""
    from vidushi_oa.mail.jmap import JmapAdapter
    tok, api, acct = (seeded_fastmail.profile.token, seeded_fastmail.api,
                      seeded_fastmail.account_id)
    # Confirm the server really errors an invalid filter at HTTP 200 (not a 4xx).
    bad = _jmap_post(api, tok, {
        "using": [CORE, MAIL],
        "methodCalls": [["Email/query",
                         {"accountId": acct, "filter": {"bogusCondition": "x"}}, "0"]]})
    assert bad["methodResponses"][0][0] == "error", bad
    assert bad["methodResponses"][0][1]["type"] == "unsupportedFilter", bad

    # Feed that real error shape through the production _parse via a transport that
    # returns the server's own error payload for the batched call.
    def error_transport(method, url, headers, body):
        if body is None:            # session GET
            return 200, _jmap_get(url, tok)
        return 200, _jmap_post(url, tok, {
            "using": [CORE, MAIL],
            "methodCalls": [
                ["Email/query",
                 {"accountId": acct, "filter": {"bogusCondition": "x"}}, "0"],
                ["Email/get",
                 {"accountId": acct,
                  "#ids": {"resultOf": "0", "name": "Email/query", "path": "/ids"},
                  "properties": ["id", "subject"]}, "1"]]})

    adapter = JmapAdapter(_account_name(seeded_fastmail.profile), "[FM]", tok,
                          session_url=seeded_fastmail.profile.jmap_url,
                          transport=error_transport)
    assert adapter.search("anything") == [], \
        "an Email/query error is no longer swallowed to an empty result"


def test_portable_query_qualifiers_are_not_translated_for_jmap(seeded_fastmail):
    """Bug 6: ``mail-search`` advertises a portable grammar (``subject:``, ``from:``,
    ``newer_than:``, ``has:attachment``, OR, groups) but NO translation layer exists —
    the raw string is dropped into the JMAP ``text`` filter verbatim. Consequences on a
    compliant server:
      * a bare qualifier (``subject:Amazon``, ``newer_than:7d``) is a literal token that
        matches NOTHING (0 ids);
      * ``newer_than`` is a silent NO-OP: ``Amazon newer_than:7d`` still returns the
        40-day-old Amazon invoice — the date bound has zero effect."""
    tok, api, acct = (seeded_fastmail.profile.token, seeded_fastmail.api,
                      seeded_fastmail.account_id)
    assert _query_ids(api, tok, acct, {"text": "subject:Amazon"}) == []
    assert _query_ids(api, tok, acct, {"text": "newer_than:7d"}) == []
    assert _query_ids(api, tok, acct, {"text": "newer_than:1w"}) == []
    # newer_than had no effect: BOTH Amazon messages (incl. the 40-day-old invoice) match.
    assert len(_query_ids(api, tok, acct, {"text": "Amazon newer_than:7d"})) == 2


# ===========================================================================
# VOA-SURFACE OBSERVATIONS — what the real verbs actually return today.
# ===========================================================================
def test_mail_search_absent_keyword_is_a_clean_empty(seeded_fastmail, tmp_path):
    """An absent keyword returns the definitive empty state (``[]``, exit 0) — the one
    read that is CORRECT today, since an empty result is right either way. (It does not
    discriminate the bug; the discriminating case is the xfail below.)"""
    env = _voa_env(tmp_path)
    account = _register_read(env, seeded_fastmail.profile)
    r = _voa(env, "mail-search", "ZZZ-absent-keyword-xyzzy", "--accounts", account)
    assert r.returncode == 0, f"{r.stdout!r} {r.stderr!r}"
    assert json.loads(r.stdout) == []


@pytest.mark.xfail(strict=True, reason=(
    "Bug 5a/5b: JmapAdapter requests the non-RFC-8621 `deliveredTo` property; the "
    "compliant server errors the Email/get and `_parse` swallows it, so mail-search "
    "returns [] despite 2 real matches. Remove deliveredTo from _EMAIL_PROPERTIES "
    "(or fetch it via a header projection) to fix."))
def test_mail_search_returns_the_seeded_amazon_rows(seeded_fastmail, tmp_path):
    """CONTRACT: ``voa mail-search Amazon`` must return the two seeded Amazon rows with
    decoded subjects. XFAIL today (swallow); XPASS the moment the adapter is fixed."""
    env = _voa_env(tmp_path)
    account = _register_read(env, seeded_fastmail.profile)
    r = _voa(env, "mail-search", "Amazon", "--accounts", account)
    assert r.returncode == 0, f"{r.stdout!r} {r.stderr!r}"
    rows = json.loads(r.stdout)
    subjects = sorted(row["subject"] for row in rows)
    assert subjects == ["Amazon invoice for your recent order",
                        "Your Amazon.in order has shipped"], rows


@pytest.mark.xfail(strict=True, reason=(
    "Bug 7: mail-get uses the same deliveredTo projection, so Email/get errors -> "
    "fetch_message returns None -> 'message not found'. (Even once fetched, note "
    "_mail_row projects only id/uid/account/source_tag/subject/sender/date — no to/cc "
    "and no body; JMAP also never populates `uid`, so a mail-search row cannot supply "
    "the id mail-get needs. This contract asserts only the fetch-by-id succeeds.)"))
def test_mail_get_opens_a_seeded_message_by_jmap_id(seeded_fastmail, tmp_path):
    """CONTRACT: ``voa mail-get --uid <jmap-id>`` must resolve a seeded message and
    return its decoded subject. XFAIL today (deliveredTo swallow -> not found)."""
    env = _voa_env(tmp_path)
    account = _register_read(env, seeded_fastmail.profile)
    target = seeded_fastmail.messages[0]
    r = _voa(env, "mail-get", "--account", account, "--uid", target["jmap_id"])
    assert r.returncode == 0, f"mail-get failed: {r.stdout!r} {r.stderr!r}"
    row = json.loads(r.stdout)
    assert row["subject"] == target["subject"], row


@pytest.mark.xfail(strict=True, reason=(
    "Same deliveredTo swallow: the round-trip message is delivered (proven over raw "
    "JMAP in the seed) but mail-search cannot surface it."))
def test_smtp_roundtrip_message_is_findable_via_mail_search(seeded_fastmail, tmp_path):
    """CONTRACT: the SMTP-delivered round-trip message is findable through the real
    ``voa mail-search`` verb. XFAIL today; the seed already proved it was delivered."""
    assert seeded_fastmail.smtp_delivered, "precondition: the message was delivered"
    env = _voa_env(tmp_path)
    account = _register_read(env, seeded_fastmail.profile)
    r = _voa(env, "mail-search", seeded_fastmail.smtp_marker, "--accounts", account)
    assert r.returncode == 0, f"{r.stdout!r} {r.stderr!r}"
    rows = json.loads(r.stdout)
    assert any(seeded_fastmail.smtp_marker in row["subject"] for row in rows), rows
