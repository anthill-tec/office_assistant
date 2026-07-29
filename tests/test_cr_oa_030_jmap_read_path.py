"""CR-OA-030 §S1/§S2 — JMAP read-path correctness (RED).

§S1 — drop the non-conformant `deliveredTo` JMAP projection.

A compliant JMAP server rejects an `Email/get` projection that requests
`deliveredTo` (not an RFC 8621 `Email` property) with a method-level
`["error", {"type": "invalidArguments", "description": "Invalid property
deliveredTo"}, callId]` INSIDE HTTP 200 — which is exactly what `_parse()`
silently swallows to an empty list today. §S1 replaces `deliveredTo` with the
conformant delivered-to **header projection** (`header:Delivered-To:asText:all`)
and makes `_build_message` read `Message.delivered_to` from that header key
instead. See `docs/changes/CR-OA-030-jmap-read-path-correctness.md` §S1 and
`docs/research/DN-mail-access.md` §"Decision 2 — revision (2026-07-29)".

The §S1 tests below are already GREEN on this branch (C1).

§S2 — surface method-level JMAP errors instead of collapsing to an empty result.

`search()` batches `["Email/query", ...]` with a back-referenced
`["Email/get", ...]`. A method-level error answers INSIDE HTTP 200 as
`["error", {"type": ..., "description": ...}, callId]` and the back-referenced
`Email/get` never runs. `_parse()` today scans `methodResponses` for an
`Email/get` response and returns `[]` when it finds none — silently reporting a
server error as a legitimate empty result at exit 0 (the field-reported blocking
bug: an agent reads `count: 0` as "no mail matched"). The §S2 tests below target
the current (defective) `search()`/`_parse()`/`cmd_mail_search` and are expected
to FAIL: a method-level error or a missing `Email/get` response is swallowed to
`[]` instead of raised, and the pre-existing `["error", ...]` literal handling in
`_created_id` is duplicated (not shared) across the module.

No real network — a small in-file `FakeTransport` records every
`(method, url, headers, body)` call and returns canned `(status, dict)`
tuples, matching the seam `tests/test_cr_oa_020_jmap.py` already uses.
"""
import inspect
import json
import re
import unittest
from argparse import Namespace

import pytest

import vidushi_oa._cli as cli
from vidushi_oa import toon as oa_toon
from vidushi_oa.mail import jmap
from vidushi_oa.mail.client import MailClient
from vidushi_oa.mail.jmap import JmapAdapter

SESSION_URL = "https://api.fastmail.com/jmap/session"
API_URL = "https://api.fastmail.com/jmap/api/"
ACCOUNT_ID = "u1234567"

_CANNED_SESSION = {
    "apiUrl": API_URL,
    "accounts": {
        ACCOUNT_ID: {
            "name": "you@fastmail.com",
            "isPersonal": True,
            "accountCapabilities": {"urn:ietf:params:jmap:mail": {}},
        },
    },
    "primaryAccounts": {"urn:ietf:params:jmap:mail": ACCOUNT_ID},
    "capabilities": {"urn:ietf:params:jmap:mail": {}},
}


def _canned_email_get_response(ids=("Ma1",)):
    """A canned batched `methodResponses` body carrying the conformant header
    projection key (NOT `deliveredTo`) for the one known id."""
    items = {
        "Ma1": {
            "id": "Ma1",
            "threadId": "Ta1",
            "messageId": ["<msg1@example.com>"],
            "subject": "Invoice from Acme",
            "from": [{"name": "Acme Billing", "email": "billing@acme.example"}],
            "to": [{"name": "Alex Doe", "email": "you@fastmail.com"}],
            "receivedAt": "2026-07-20T10:00:00Z",
            "header:Delivered-To:asText:all": "purchases-alias@fastmail.com",
        },
    }
    return {
        "methodResponses": [
            ["Email/query", {"accountId": ACCOUNT_ID, "ids": list(ids)}, "0"],
            [
                "Email/get",
                {
                    "accountId": ACCOUNT_ID,
                    "list": [items[i] for i in ids],
                    "notFound": [],
                },
                "1",
            ],
        ],
    }


class FakeTransport:
    """Records every `(method, url, headers, body)` call and returns canned
    `(status, dict)` tuples shaped like real JMAP HTTP responses — no network."""

    def __init__(self, session_response=None, post_response=None):
        self.session_response = session_response or _CANNED_SESSION
        self.post_response = post_response or _canned_email_get_response()
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        if method == "GET":
            return 200, self.session_response
        return 200, self.post_response

    def calls_of(self, method):
        return [c for c in self.calls if c[0] == method]


class EmailPropertiesProjectionTest(unittest.TestCase):
    """§S1 AC1: `_EMAIL_PROPERTIES` contains no `deliveredTo` entry; every
    remaining entry is an RFC 8621 `Email` property or the delivered-to
    header projection (`header:Delivered-To:asText:all`). Fails today because
    `deliveredTo` is present and the header projection is absent."""

    def test_email_properties_excludes_deliveredto(self):
        self.assertNotIn("deliveredTo", jmap._EMAIL_PROPERTIES)

    def test_email_properties_matches_the_exact_conformant_set(self):
        self.assertEqual(
            set(jmap._EMAIL_PROPERTIES),
            {
                "id",
                "threadId",
                "messageId",
                "subject",
                "from",
                "to",
                "receivedAt",
                "header:Delivered-To:asText:all",
            },
        )


class BuildMessageDeliveredToFromHeaderProjectionTest(unittest.TestCase):
    """§S1 AC2: `_build_message` populates `Message.delivered_to` from the
    header projection key (`header:Delivered-To:asText:all`) when present in
    the `Email/get` item, and `""` when absent — with no raise. Fails today
    because `_build_message` reads `item.get("deliveredTo")` instead."""

    def setUp(self):
        self.adapter = JmapAdapter(
            account="fastmail_main", source_tag="[FM]", token="secret-token",
            session_url=SESSION_URL, transport=FakeTransport(),
        )

    def test_delivered_to_is_populated_from_the_header_projection_key_when_present(self):
        item = {
            "id": "Ma1",
            "threadId": "Ta1",
            "messageId": ["<msg1@example.com>"],
            "subject": "Invoice from Acme",
            "from": [{"name": "Acme Billing", "email": "billing@acme.example"}],
            "to": [{"name": "Alex Doe", "email": "you@fastmail.com"}],
            "receivedAt": "2026-07-20T10:00:00Z",
            "header:Delivered-To:asText:all": "alias@example.com",
        }

        message = self.adapter._build_message(item)

        self.assertEqual(message.delivered_to, "alias@example.com")

    def test_delivered_to_is_empty_string_when_header_projection_key_absent(self):
        # The item carries the OLD, non-conformant `deliveredTo` property (as a
        # non-compliant/legacy server response might) but NOT the new header
        # projection key. `_build_message` must ignore `deliveredTo` entirely
        # post-fix and degrade to "" — proving it reads the header key, not
        # the retired property. Today's code reads `item.get("deliveredTo")`
        # and would wrongly return the legacy value instead of "".
        item = {
            "id": "Ma2",
            "threadId": "Ta2",
            "messageId": ["<msg2@example.com>"],
            "subject": "Your receipt",
            "from": [{"name": "Shop Receipts", "email": "receipts@shop.example"}],
            "to": [{"name": "Alex Doe", "email": "you@fastmail.com"}],
            "receivedAt": "2026-07-21T11:00:00Z",
            "deliveredTo": "stale-legacy-value@fastmail.com",
        }

        message = self.adapter._build_message(item)

        self.assertEqual(message.delivered_to, "")


class BuildMessageDeliveredToArrayShapeTest(unittest.TestCase):
    """§S1 AC2 (real-server shape): RFC 8621 §4.1.4 defines the `:all` suffix as
    returning a JSON ARRAY of strings — every header field with that name — not
    a scalar. Every fake in `tests/` hands `_build_message` a plain string, so
    the unit tier cannot see that a real server's array value lands on
    `Message.delivered_to` verbatim as a `list`. `delivered_to` must ALWAYS be a
    `str`: a list/tuple collapses to its first non-empty (stripped) entry, a
    plain string passes through, and absent/None/empty degrades to `""`."""

    def setUp(self):
        self.adapter = JmapAdapter(
            account="fastmail_main", source_tag="[FM]", token="secret-token",
            session_url=SESSION_URL, transport=FakeTransport(),
        )

    def _item(self, delivered_to_value):
        item = {
            "id": "Ma1",
            "threadId": "Ta1",
            "messageId": ["<msg1@example.com>"],
            "subject": "Invoice from Acme",
            "from": [{"name": "Acme Billing", "email": "billing@acme.example"}],
            "to": [{"name": "Alex Doe", "email": "you@fastmail.com"}],
            "receivedAt": "2026-07-20T10:00:00Z",
        }
        if delivered_to_value is not None:
            item["header:Delivered-To:asText:all"] = delivered_to_value
        return item

    def test_single_element_array_yields_that_address_as_a_string(self):
        message = self.adapter._build_message(self._item(["alias@example.com"]))

        self.assertIsInstance(message.delivered_to, str)
        self.assertEqual(message.delivered_to, "alias@example.com")

    def test_multi_value_array_yields_the_first_address_as_a_string(self):
        message = self.adapter._build_message(
            self._item(["first@example.com", "second@example.com"]))

        self.assertIsInstance(message.delivered_to, str)
        self.assertEqual(message.delivered_to, "first@example.com")

    def test_array_entries_are_stripped_and_empty_leading_entries_skipped(self):
        # `asText` values carry the header's leading fold whitespace; an empty
        # first field must not mask a real address behind it.
        message = self.adapter._build_message(
            self._item(["", "  alias@example.com  "]))

        self.assertEqual(message.delivered_to, "alias@example.com")

    def test_empty_array_yields_the_empty_string(self):
        message = self.adapter._build_message(self._item([]))

        self.assertIsInstance(message.delivered_to, str)
        self.assertEqual(message.delivered_to, "")

    def test_all_blank_array_yields_the_empty_string(self):
        message = self.adapter._build_message(self._item(["", "   "]))

        self.assertIsInstance(message.delivered_to, str)
        self.assertEqual(message.delivered_to, "")

    def test_none_value_yields_the_empty_string(self):
        # A server that saw no Delivered-To field returns JSON `null`.
        message = self.adapter._build_message(self._item(None))
        self.assertEqual(message.delivered_to, "")

        explicit_null = self._item(["placeholder"])
        explicit_null["header:Delivered-To:asText:all"] = None
        self.assertEqual(self.adapter._build_message(explicit_null).delivered_to, "")

    def test_plain_string_still_passes_through_stripped(self):
        message = self.adapter._build_message(self._item(" alias@example.com "))

        self.assertIsInstance(message.delivered_to, str)
        self.assertEqual(message.delivered_to, "alias@example.com")


class SearchRequestProjectionConformanceTest(unittest.TestCase):
    """§S1 AC3 (request-shape guard): the `properties` list our adapter SENDS
    in the `Email/get` half of `search()`'s batched request is conformant — no
    `deliveredTo`, but does carry the header projection. The CR-OA-020 fakes
    only ever proved we can PARSE a `deliveredTo` field in a canned response,
    never that a real server accepts it in an outgoing request. Fails today
    because `_EMAIL_PROPERTIES` (sent verbatim as `properties`) still carries
    `deliveredTo` and lacks the header projection."""

    def test_search_sends_a_conformant_email_get_properties_projection(self):
        transport = FakeTransport()
        adapter = JmapAdapter(
            account="fastmail_main", source_tag="[FM]", token="secret-token",
            session_url=SESSION_URL, transport=transport,
        )

        adapter.search("invoice")

        _, _, _, body = transport.calls_of("POST")[0]
        get_call = body["methodCalls"][1]
        self.assertEqual(get_call[0], "Email/get")
        properties = get_call[1]["properties"]
        self.assertNotIn("deliveredTo", properties)
        self.assertIn("header:Delivered-To:asText:all", properties)


# ─────────────────────────── §S2 canned method-level responses ───────────────────────────

def _canned_method_error_response():
    """A batched response where the `Email/get` half (`callId "1"`) never ran
    because the server rejected the request with a method-level error — the
    real-world shape §S2 targets: `Email/query` succeeds, but its back-referenced
    `Email/get` answers `["error", {...}, "1"]` instead of an `Email/get` result."""
    return {
        "methodResponses": [
            ["Email/query", {"accountId": ACCOUNT_ID, "ids": ["Ma1"]}, "0"],
            [
                "error",
                {"type": "invalidArguments", "description": "Invalid property deliveredTo"},
                "1",
            ],
        ],
    }


def _canned_missing_email_get_response():
    """A batched response carrying only the `Email/query` result — no `Email/get`
    response AND no `["error", ...]` entry at all (a malformed/truncated server
    answer), so the only clue anything is wrong is the absent back-reference
    result."""
    return {
        "methodResponses": [
            ["Email/query", {"accountId": ACCOUNT_ID, "ids": ["Ma1"]}, "0"],
        ],
    }


def _canned_legitimate_empty_response():
    """A genuinely empty search: `Email/query` answers `ids: []` and the
    back-referenced `Email/get` answers a matching empty `list`/`notFound` — no
    error anywhere. This must stay a clean `[]`, never raise."""
    return {
        "methodResponses": [
            ["Email/query", {"accountId": ACCOUNT_ID, "ids": []}, "0"],
            [
                "Email/get",
                {"accountId": ACCOUNT_ID, "list": [], "notFound": []},
                "1",
            ],
        ],
    }


class SearchRaisesOnMethodLevelErrorResponseTest(unittest.TestCase):
    """§S2 AC1: given a response whose `methodResponses` carry
    `["error", {"type": "invalidArguments", ...}, "1"]`, `JmapAdapter.search(...)`
    raises (does NOT return `[]`), and the raised message contains BOTH the
    server's `type` and its `description`. Fails today: `_parse()` finds no
    `Email/get` response and silently returns `[]`."""

    def test_search_raises_naming_both_the_servers_error_type_and_description(self):
        transport = FakeTransport(post_response=_canned_method_error_response())
        adapter = JmapAdapter(
            account="fastmail_main", source_tag="[FM]", token="secret-token",
            session_url=SESSION_URL, transport=transport,
        )

        with self.assertRaises(RuntimeError) as ctx:
            adapter.search("invoice")

        message = str(ctx.exception)
        self.assertIn("invalidArguments", message)
        self.assertIn("Invalid property deliveredTo", message)


class SearchRaisesWhenEmailGetResponseIsMissingTest(unittest.TestCase):
    """§S2 AC2: given a response with a valid `Email/query` result but NO
    `Email/get` response at all, `search()` raises naming the missing `Email/get`
    — not `[]`. Fails today: `_parse()` returns `[]` when it finds no `Email/get`
    response, with no raise and no mention of what's missing."""

    def test_search_raises_naming_the_missing_email_get_response(self):
        transport = FakeTransport(post_response=_canned_missing_email_get_response())
        adapter = JmapAdapter(
            account="fastmail_main", source_tag="[FM]", token="secret-token",
            session_url=SESSION_URL, transport=transport,
        )

        with self.assertRaises(RuntimeError) as ctx:
            adapter.search("invoice")

        self.assertIn("Email/get", str(ctx.exception))


class SearchDistinguishesLegitimateEmptyFromFailureTest(unittest.TestCase):
    """§S2 AC3: an `Email/query` answering `ids: []` with a matching empty
    `Email/get` result must return `[]` WITHOUT raising — but that same `[]`
    must NOT be what a genuinely FAILED search (a missing `Email/get`
    response, AC2's shape) also returns; the two must be distinguishable.
    Fails today: `_parse()` returns the identical `[]` for BOTH inputs — a
    legitimate empty and a broken response collapse to the same result."""

    def test_legitimate_empty_stays_clean_but_a_missing_response_raises(self):
        clean_adapter = JmapAdapter(
            account="fastmail_main", source_tag="[FM]", token="secret-token",
            session_url=SESSION_URL,
            transport=FakeTransport(post_response=_canned_legitimate_empty_response()),
        )
        self.assertEqual(clean_adapter.search("nonexistent-vendor-xyz"), [])

        broken_adapter = JmapAdapter(
            account="fastmail_main", source_tag="[FM]", token="secret-token",
            session_url=SESSION_URL,
            transport=FakeTransport(post_response=_canned_missing_email_get_response()),
        )
        with self.assertRaises(RuntimeError):
            broken_adapter.search("invoice")


@pytest.fixture(autouse=True)
def _restore_cli_fmt_cr_oa_030():
    """`cmd_mail_search` reads the module-global `cli._FMT`; these §S2 CLI tests
    mutate it directly, so restore it afterwards (matches the fixture of the same
    purpose in `tests/test_cr_oa_020_mail_verbs.py`)."""
    original = getattr(cli, "_FMT", "toon")
    yield
    cli._FMT = original


def test_mail_search_cli_distinguishes_a_legitimate_empty_result_from_a_rejected_request(
        monkeypatch, capsys):
    """§S2 AC3 (CLI half) + AC4: `voa mail-search` against a JMAP account whose
    server legitimately matched nothing (`Email/query` -> `ids: []`, matching
    empty `Email/get`) must print `count: 0` at exit 0 — but against a server
    that REJECTED the request (a method-level error inside HTTP 200) must exit
    NON-ZERO with a structured error payload and NO traceback (AXI #6), never
    the same `count: 0`/exit-0 shape. Exercised through `cli.cmd_mail_search`
    (the `cli.build_client` seam), matching `tests/test_cr_oa_020_mail_verbs.py`'s
    conventions. Fails today: a rejected request also exits 0 printing
    `count: 0`, indistinguishable from the genuinely empty case."""
    clean_adapter = JmapAdapter(
        account="fastmail_main", source_tag="[FM]", token="secret-token",
        session_url=SESSION_URL,
        transport=FakeTransport(post_response=_canned_legitimate_empty_response()),
    )
    monkeypatch.setattr(
        cli, "build_client",
        lambda **kw: MailClient({"fastmail_main": clean_adapter}))
    cli._FMT = "toon"
    cli.cmd_mail_search(Namespace(query="nonexistent-vendor-xyz", accounts=["fastmail_main"]))
    captured = capsys.readouterr()
    assert "Traceback" not in captured.out and "Traceback" not in captured.err
    clean_payload = oa_toon.from_toon(captured.out)
    assert clean_payload["count"] == 0
    assert clean_payload["results"] == []

    rejecting_adapter = JmapAdapter(
        account="fastmail_main", source_tag="[FM]", token="secret-token",
        session_url=SESSION_URL,
        transport=FakeTransport(post_response=_canned_method_error_response()),
    )
    monkeypatch.setattr(
        cli, "build_client",
        lambda **kw: MailClient({"fastmail_main": rejecting_adapter}))
    cli._FMT = "json"

    with pytest.raises(SystemExit) as exc_info:
        cli.cmd_mail_search(Namespace(query="invoice", accounts=["fastmail_main"]))

    assert exc_info.value.code != 0
    captured = capsys.readouterr()
    assert "Traceback" not in captured.out and "Traceback" not in captured.err
    rejected_payload = json.loads(captured.out.strip())
    assert "error" in rejected_payload


class SharedMethodErrorCheckIsNotDuplicatedTest(unittest.TestCase):
    """§S2 AC5: a mechanically auditable check that ONE reusable method-level
    error check backs both the search/`_parse` read path and the pre-existing
    `_created_id` write path — no duplicated `["error", ...]` literal handling.

    Fails today: `_created_id`, `_queried_ids`, and `_identity_id` each carry
    their OWN separate `response[0] == "error"` comparison (three duplicated
    literal sites), and `_parse`/`search` perform no such check at all."""

    def _module_source(self):
        return inspect.getsource(jmap)

    def test_exactly_one_literal_error_tuple_comparison_site_in_the_module(self):
        source = self._module_source()
        literal_hits = re.findall(r'\[0\]\s*==\s*"error"', source)
        self.assertEqual(
            len(literal_hits), 1,
            f"expected exactly one reusable `[0] == \"error\"` check in "
            f"vidushi_oa/mail/jmap.py, found {len(literal_hits)} duplicated "
            "literal sites")

    def test_the_shared_check_is_called_from_both_created_id_and_the_search_path(self):
        source = self._module_source()
        match = re.search(r'\[0\]\s*==\s*"error"', source)
        self.assertIsNotNone(match, "no method-level error check found at all")
        preceding = source[:match.start()]
        def_matches = list(re.finditer(r"^def (\w+)\(", preceding, re.MULTILINE))
        self.assertTrue(def_matches, "the literal error check is not inside any function")
        owner_name = def_matches[-1].group(1)
        self.assertNotEqual(
            owner_name, "_created_id",
            "the error check must be factored OUT of _created_id into a shared "
            "helper that _created_id AND the read path (search/_parse) both "
            "call — not left inline inside _created_id itself")

        created_id_source = inspect.getsource(jmap._created_id)
        parse_source = inspect.getsource(JmapAdapter._parse)
        search_source = inspect.getsource(JmapAdapter.search)

        self.assertIn(
            f"{owner_name}(", created_id_source,
            f"shared check `{owner_name}` must be called from _created_id")
        self.assertTrue(
            f"{owner_name}(" in parse_source or f"{owner_name}(" in search_source,
            f"shared check `{owner_name}` must be called from search()/_parse()")


if __name__ == "__main__":
    unittest.main()
