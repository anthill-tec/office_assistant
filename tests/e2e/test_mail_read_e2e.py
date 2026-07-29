"""E2E READ-path smoke tests — the real ``voa mail-search`` / ``mail-get`` verbs against
the Stalwart JMAP (``fastmail``) emulator profile (DN-mail-e2e-emulator-testing.md).

The existing E2E tier (``test_mail_send_draft_e2e.py``) only exercised the SEND/DRAFT
path; the READ path (``mail-search`` / ``mail-get``) had never been driven end-to-end.
This module closes that gap — and in doing so reproduced a class of read-path bugs the
fakes-based suite (``tests/test_cr_oa_020_jmap.py``) structurally cannot catch. Those
bugs are CR-OA-030; this module is now their PROOF and their regression guard.

WHAT WAS BROKEN (CR-OA-030, fixed §S1–§S3 — this file is the proof it is fixed):

* §S1 ``JmapAdapter`` requested ``deliveredTo`` in its ``Email/get`` projection
  (``_EMAIL_PROPERTIES``, added in CR-OA-020 for the masked-alias trick).
  ``deliveredTo`` is NOT an RFC 8621 Email property, so a compliant server (Stalwart —
  and, per the field report, real Fastmail) rejected the whole projection: the batched
  ``Email/query`` SUCCEEDED while the paired ``Email/get`` came back as
  ``["error", {"type": "invalidArguments", "description": "…deliveredTo"}, "1"]`` inside
  an HTTP 200. The correlation key is now read from the conformant header projection
  ``header:Delivered-To:asText:all`` — which per RFC 8621 §4.1.4 answers a JSON **array**,
  normalised to a ``str``. The fakes tier structurally could not catch a list landing on a
  ``str`` field; ``test_delivered_to_is_a_string_against_the_real_server`` does, against a
  real server.
* §S2 ``_parse()`` scanned ``methodResponses`` only for an ``Email/get`` *response* and
  returned ``[]`` when it found none — so that error was SWALLOWED and every search/fetch
  collapsed to a legitimate-looking empty result (exit 0, ``count: 0``). A method-level
  error now RAISES, naming the server's ``type`` and ``description``.
* §S3 the JMAP ``_build_message`` never set ``uid``, so a ``mail-search`` row could not
  supply the id ``mail-get`` needs. ``Message.uid`` is now the JMAP ``Email`` id.

STILL OPEN (deliberately): the ``{"text": query}`` filter itself is CORRECT and matches on
Stalwart (``test_text_filter_matches_on_the_compliant_jmap_server``), but the advertised
portable/compound grammar has no translation layer at all, so ``subject:`` /
``newer_than:`` qualifiers are passed verbatim into the JMAP ``text`` filter and are
silently ineffective (a no-op on the date bound; zero hits for a bare qualifier). That is
CR-OA-031; it is pinned here by
``test_portable_query_qualifiers_are_not_translated_for_jmap``.

ISOLATION mirrors ``test_mail_send_draft_e2e.py``: every ``voa`` verb runs in a subprocess
whose registry/secret-store/data-dir are pinned into a throwaway ``tmp_path`` and pointed
at the emulator purely via the #63 endpoint override, so the user's real accounts/store
are never touched. Seeding lands messages straight in the ``fastmail`` mailbox over JMAP
``Email/import`` (full control of ``receivedAt``, an attachment, and a ``Delivered-To``
header) plus one real SMTP send→fetch round-trip. Every test is ``@pytest.mark.e2e``
(excluded by ``-m 'not e2e'``). Nothing here patches production code.
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

from vidushi_oa.mail.jmap import _EMAIL_PROPERTIES, JmapAdapter

pytestmark = pytest.mark.e2e

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
STORE = os.path.join(ROOT, "scripts", "store.py")

CORE = "urn:ietf:params:jmap:core"
MAIL = "urn:ietf:params:jmap:mail"

# The masked-alias correlation key seeded as a real ``Delivered-To`` header on ONE
# message (CR-OA-030 §S1). Chosen to share no token with any search keyword used here,
# so adding the header cannot perturb the `text`-filter counts the other tests assert.
ALIAS_DELIVERED_TO = "masked-key-7fq2@emumail.org"


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
    """Raw ``Email/query`` ids for ``jmap_filter`` — no ``Email/get``, no property
    projection, so this bypasses the adapter entirely and is the clean, independent
    ground-truth of what the server itself considers a match."""
    payload = _jmap_post(api, token, {
        "using": [CORE, MAIL],
        "methodCalls": [["Email/query",
                         {"accountId": account_id, "filter": jmap_filter}, "0"]]})
    resp = payload["methodResponses"][0]
    assert resp[0] == "Email/query", f"unexpected raw query response: {payload}"
    return resp[1].get("ids") or []


def _rfc822(subject, from_addr, to_addr, body, received=None, attach=False,
            delivered_to=None):
    msg = email.message.EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Message-ID"] = email.utils.make_msgid(domain="emumail.org")
    if received is not None:
        msg["Date"] = email.utils.format_datetime(received)
    if delivered_to is not None:
        msg["Delivered-To"] = delivered_to
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
        # subject, sender, body, age(days), attachment, Delivered-To header
        ("Your Amazon.in order has shipped", "orders@amazon.in",
         "Amazon shipment tracking AB123 out for delivery", 1, False,
         ALIAS_DELIVERED_TO),
        ("Amazon invoice for your recent order", "invoice@amazon.in",
         "Amazon tax invoice attached for your records", 40, True, None),
        ("Flipkart delivery update", "noreply@flipkart.com",
         "Your Flipkart parcel is out for delivery today", 2, False, None),
        ("Netflix payment receipt", "info@netflix.com",
         "Your Netflix subscription renewed for the month", 10, False, None),
    ]
    messages = []
    for subject, sender, body, age, attach, delivered_to in plan:
        received = now - datetime.timedelta(days=age)
        raw = _rfc822(subject, sender, fm.address, body, received=received,
                      attach=attach, delivered_to=delivered_to)
        blob = _upload_blob(upload, fm.token, raw)
        jmap_id = _import(api, fm.token, account_id, inbox_id, blob, received)
        messages.append({"subject": subject, "sender": sender, "jmap_id": jmap_id,
                         "age_days": age, "attach": attach,
                         "delivered_to": delivered_to or ""})

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
    """POSITIVE CONTROL: the ``{"text": query}`` filter our adapter emits is CORRECT
    against a compliant JMAP server — ``Amazon`` matches the two Amazon messages,
    ``Flipkart`` matches one, and an absent keyword matches none. This is what isolated
    the original 0-count field report to the projection/swallow downstream rather than
    the filter, and it stays here as the guard that keeps that attribution honest."""
    tok, api, acct = (seeded_fastmail.profile.token, seeded_fastmail.api,
                      seeded_fastmail.account_id)
    assert len(_query_ids(api, tok, acct, {"text": "Amazon"})) == 2
    assert len(_query_ids(api, tok, acct, {"text": "Flipkart"})) == 1
    assert _query_ids(api, tok, acct, {"text": "ZZZ-absent-keyword-xyzzy"}) == []


def test_email_properties_projection_is_accepted_by_the_compliant_jmap_server(
        seeded_fastmail):
    """§S1 AC-3: the EXACT batched body ``JmapAdapter.search()`` sends — an
    ``Email/query {text}`` back-referenced into an ``Email/get`` requesting the real
    ``_EMAIL_PROPERTIES`` — is now ACCEPTED whole: both halves answer, neither is an
    ``["error", …]`` methodResponse. This is the direct inverse of the pre-fix root
    cause, where the same body had ``Email/query`` succeed while the paired
    ``Email/get`` failed with ``invalidArguments`` naming ``deliveredTo``.

    It also pins the projection itself: no ``deliveredTo``, and the delivered-to
    correlation key carried by a conformant ``header:…`` projection instead."""
    assert "deliveredTo" not in _EMAIL_PROPERTIES, _EMAIL_PROPERTIES
    assert "header:Delivered-To:asText:all" in _EMAIL_PROPERTIES, _EMAIL_PROPERTIES
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
    assert by_call["1"][0] == "Email/get", f"Email/get errored: {responses}"
    assert len(by_call["1"][1]["list"]) == 2, by_call["1"]


def test_search_surfaces_the_email_get_error(seeded_fastmail):
    """§S2, ``Email/get`` arm: when the server answers the back-referenced
    ``Email/get`` with a method-level error inside an HTTP 200, the PRODUCTION
    ``JmapAdapter.search()`` now RAISES naming the server's ``type`` AND
    ``description`` — it no longer swallows the failure into a legitimate-looking
    empty result.

    Driven with an injected transport that reissues the batched call with the RETIRED
    ``deliveredTo`` projection — the exact request the pre-fix adapter sent, and still
    the cleanest way to make a compliant server error a back-referenced ``Email/get``
    for real. The swallow lived in ``_parse``, which this exercises on the server's own
    error payload."""
    tok, api, acct = (seeded_fastmail.profile.token, seeded_fastmail.api,
                      seeded_fastmail.account_id)
    # Ground truth: 2 really match, so an empty return could only ever be the swallow.
    assert len(_query_ids(api, tok, acct, {"text": "Amazon"})) == 2

    def error_transport(method, url, headers, body):
        if body is None:            # session GET
            return 200, _jmap_get(url, tok)
        return 200, _jmap_post(url, tok, {
            "using": [CORE, MAIL],
            "methodCalls": [
                ["Email/query", {"accountId": acct, "filter": {"text": "Amazon"}}, "0"],
                ["Email/get",
                 {"accountId": acct,
                  "#ids": {"resultOf": "0", "name": "Email/query", "path": "/ids"},
                  "properties": ["id", "deliveredTo"]}, "1"]]})

    adapter = JmapAdapter(_account_name(seeded_fastmail.profile), "[FM]", tok,
                          session_url=seeded_fastmail.profile.jmap_url,
                          transport=error_transport)
    with pytest.raises(RuntimeError) as excinfo:
        adapter.search("Amazon")
    rendered = str(excinfo.value)
    assert "invalidArguments" in rendered, rendered
    assert "deliveredTo" in rendered, rendered


def test_forced_query_error_is_surfaced(seeded_fastmail):
    """§S2, ``Email/query`` arm: an INVALID filter makes ``Email/query`` ITSELF error
    (``unsupportedFilter``) at HTTP 200, and the back-referenced ``Email/get`` therefore
    never runs. ``search()`` now RAISES naming that error rather than collapsing to
    ``[]`` — a server/filter failure is no longer indistinguishable from a legitimately
    empty result at the voa surface. (Driven with an injected transport because
    ``search()`` only ever emits a valid ``{text}`` filter.)"""
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
    with pytest.raises(RuntimeError) as excinfo:
        adapter.search("anything")
    assert "unsupportedFilter" in str(excinfo.value), str(excinfo.value)


def test_delivered_to_is_a_string_against_the_real_server(seeded_fastmail):
    """§S1 CARRY-IN — the assertion that would have caught the array bug automatically.

    ``header:Delivered-To:asText:all`` returns a JSON **ARRAY** per RFC 8621 §4.1.4, and
    ``Message.delivered_to`` is a ``str`` field. The fakes tier structurally could not
    catch a list landing there (its canned payloads decide the shape); only reading the
    RFC did. This closes that hole at the one place the shape is decided by a real,
    compliant server:

    * ``isinstance(..., str)`` — NOT merely truthy/non-empty. A raw ``["addr"]`` list is
      truthy and non-empty, so a truthiness assertion would have passed on the bug.
    * the message SEEDED WITH a ``Delivered-To`` header resolves to that header's value
      (the masked-alias correlation key survives the array→str normalisation);
    * the message seeded WITHOUT one degrades to ``""`` — a ``str``, never ``None`` and
      never a list — which is the documented §S1 no-raise fallback."""
    adapter = JmapAdapter(
        _account_name(seeded_fastmail.profile), "[FM]", seeded_fastmail.profile.token,
        session_url=seeded_fastmail.profile.jmap_url)
    messages = adapter.search("Amazon")
    assert len(messages) == 2, messages
    for message in messages:
        assert isinstance(message.delivered_to, str), (
            f"delivered_to is {type(message.delivered_to).__name__}, not str: "
            f"{message.delivered_to!r} — the RFC 8621 ':all' array leaked through")
    by_subject = {message.subject: message for message in messages}
    seeded = {row["subject"]: row for row in seeded_fastmail.messages}

    tagged = by_subject["Your Amazon.in order has shipped"]
    assert seeded["Your Amazon.in order has shipped"]["delivered_to"] == \
        ALIAS_DELIVERED_TO, "precondition: this message was seeded WITH the header"
    assert tagged.delivered_to == ALIAS_DELIVERED_TO, tagged.delivered_to

    untagged = by_subject["Amazon invoice for your recent order"]
    assert seeded["Amazon invoice for your recent order"]["delivered_to"] == "", \
        "precondition: this message was seeded WITHOUT the header"
    assert untagged.delivered_to == "", untagged.delivered_to


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


def test_mail_search_returns_the_seeded_amazon_rows(seeded_fastmail, tmp_path):
    """CONTRACT (§S4): ``voa mail-search Amazon`` returns the two seeded Amazon rows
    with decoded subjects. Was ``xfail(strict=True)`` while the ``deliveredTo``
    projection made the server error the ``Email/get`` and ``_parse`` swallow it to
    ``[]``; it PASSES now, against the real server."""
    env = _voa_env(tmp_path)
    account = _register_read(env, seeded_fastmail.profile)
    r = _voa(env, "mail-search", "Amazon", "--accounts", account)
    assert r.returncode == 0, f"{r.stdout!r} {r.stderr!r}"
    rows = json.loads(r.stdout)
    subjects = sorted(row["subject"] for row in rows)
    assert subjects == ["Amazon invoice for your recent order",
                        "Your Amazon.in order has shipped"], rows


def test_mail_get_opens_a_seeded_message_by_jmap_id(seeded_fastmail, tmp_path):
    """CONTRACT (§S4): ``voa mail-get --uid <jmap-id>`` resolves a seeded message and
    returns its decoded subject. Was ``xfail(strict=True)`` — ``mail-get`` used the same
    ``deliveredTo`` projection, so the ``Email/get`` errored, ``fetch_message`` returned
    ``None`` and the verb reported "message not found". (``_mail_row`` still projects
    only id/uid/account/source_tag/subject/sender/date — no to/cc and no body; that is
    CR-OA-032. This contract asserts the fetch-by-id succeeds.)"""
    env = _voa_env(tmp_path)
    account = _register_read(env, seeded_fastmail.profile)
    target = seeded_fastmail.messages[0]
    r = _voa(env, "mail-get", "--account", account, "--uid", target["jmap_id"])
    assert r.returncode == 0, f"mail-get failed: {r.stdout!r} {r.stderr!r}"
    row = json.loads(r.stdout)
    assert row["subject"] == target["subject"], row


def test_smtp_roundtrip_message_is_findable_via_mail_search(seeded_fastmail, tmp_path):
    """CONTRACT (§S4): the SMTP-delivered round-trip message is findable through the
    real ``voa mail-search`` verb. Was ``xfail(strict=True)`` under the same swallow —
    the seed had already proven over raw JMAP that the message was delivered."""
    assert seeded_fastmail.smtp_delivered, "precondition: the message was delivered"
    env = _voa_env(tmp_path)
    account = _register_read(env, seeded_fastmail.profile)
    r = _voa(env, "mail-search", seeded_fastmail.smtp_marker, "--accounts", account)
    assert r.returncode == 0, f"{r.stdout!r} {r.stderr!r}"
    rows = json.loads(r.stdout)
    assert any(seeded_fastmail.smtp_marker in row["subject"] for row in rows), rows


def test_mail_search_row_uid_resolves_via_mail_get(seeded_fastmail, tmp_path):
    """§S3 end-to-end (CR-OA-026 parity): a JMAP ``mail-search`` row carries a NON-NULL
    ``uid`` equal to the message's JMAP ``Email`` id, and feeding that uid straight back
    into ``voa mail-get --account <a> --uid <uid>`` resolves the same message at exit 0.

    Pre-fix the JMAP ``_build_message`` never set ``uid``, so the search→get handoff the
    CLI itself advertises (``next: mail-get --account … --uid …``) was unusable on a
    Fastmail account regardless of the projection bug. ``Flipkart`` is used because it
    matches exactly one seeded message, making the row unambiguous."""
    env = _voa_env(tmp_path)
    account = _register_read(env, seeded_fastmail.profile)
    r = _voa(env, "mail-search", "Flipkart", "--accounts", account)
    assert r.returncode == 0, f"{r.stdout!r} {r.stderr!r}"
    rows = json.loads(r.stdout)
    assert len(rows) == 1, rows
    uid = rows[0]["uid"]
    assert uid, f"the JMAP row carries no uid — mail-get cannot be driven from it: {rows}"
    expected = next(m for m in seeded_fastmail.messages
                    if m["subject"] == "Flipkart delivery update")
    assert uid == expected["jmap_id"], (uid, expected["jmap_id"])

    got = _voa(env, "mail-get", "--account", account, "--uid", uid)
    assert got.returncode == 0, f"mail-get failed: {got.stdout!r} {got.stderr!r}"
    fetched = json.loads(got.stdout)
    assert fetched["subject"] == expected["subject"], fetched
    assert fetched["uid"] == uid, fetched
