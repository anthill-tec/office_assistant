"""CR-OA-031 §S2 — JMAP `Email/query` filter compilation from the portable query
model (RED).

Today `JmapAdapter.search(query, ...)` sends the raw query string as ONE opaque
`{"text": query}` blob (`vidushi_oa/mail/jmap.py` ~line 431), so a qualifier like
`subject:Amazon` is never recognised by the server -- it is literally matched as
free text and hits nothing -- and `newer_than:` has zero effect on what the
server returns (a silent no-op: a stale message stays in the result set). §S2
fixes this by compiling `vidushi_oa.mail.query.parse(query)`'s `QueryModel` into
real RFC 8621 `FilterCondition`s BEFORE the request is sent:

    terms            -> {"text": <term>}         (one condition per bare term)
    subject:<v>       -> {"subject": <v>}
    from:<v>          -> {"from": <v>}
    to:<v>            -> {"to": <v>}
    has:attachment    -> {"hasAttachment": true}
    newer_than:<date>  -> {"after": "<ISO-8601 UTCDate cutoff>"}
    multiple conditions, implicit-AND -> {"operator": "AND", "conditions": [...]}
    multiple conditions, `OR`          -> {"operator": "OR", "conditions": [...]}

These tests assert on the actual `Email/query` REQUEST BODY `JmapAdapter.search()`
sends (via the existing fake-transport seam `tests/test_cr_oa_030_jmap_read_path.py`
already uses), never on the parser directly -- proving the adapter actually WIRES
the compiled filter into the wire request, not just that a compiler function exists
somewhere unused.

Convention asserted for `newer_than:` (not spelled out verbatim by the CR, but the
only unambiguous reading of "ISO-8601 cutoff" for JMAP's `UTCDate` type, which
requires a full date-time, not a bare date): the model's absolute cutoff `date` is
sent as MIDNIGHT UTC of that date, e.g. `2024-03-08T00:00:00Z`. If GREEN adopts a
different (but still spec-compliant) time-of-day convention, this assertion is the
one to update -- it is not a guess left unresolved, it is a documented design choice
this test pins.

No real network -- the same in-file `FakeTransport` shape as
`tests/test_cr_oa_030_jmap_read_path.py` records every `(method, url, headers,
body)` call and returns canned `(status, dict)` tuples.
"""
import unittest
from datetime import date, timedelta

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

#: A minimal batched response that lets `_parse()` succeed cleanly (an empty,
#: legitimate result) -- these tests only care about the REQUEST body `search()`
#: sends, never the parsed return value.
_CANNED_EMPTY_RESULT = {
    "methodResponses": [
        ["Email/query", {"accountId": ACCOUNT_ID, "ids": []}, "0"],
        ["Email/get", {"accountId": ACCOUNT_ID, "list": [], "notFound": []}, "1"],
    ],
}


class FakeTransport:
    """Records every `(method, url, headers, body)` call and returns canned
    `(status, dict)` tuples shaped like real JMAP HTTP responses -- no network."""

    def __init__(self, session_response=None, post_response=None):
        self.session_response = session_response or _CANNED_SESSION
        self.post_response = post_response or _CANNED_EMPTY_RESULT
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        if method == "GET":
            return 200, self.session_response
        return 200, self.post_response

    def calls_of(self, method):
        return [c for c in self.calls if c[0] == method]


def _sent_filter(query):
    """Run `JmapAdapter.search(query)` against a fresh `FakeTransport` and return
    the `filter` object the adapter placed in the `Email/query` half of its
    batched POST request."""
    transport = FakeTransport()
    adapter = JmapAdapter(
        account="fastmail_main", source_tag="[FM]", token="secret-token",
        session_url=SESSION_URL, transport=transport,
    )
    adapter.search(query)
    _, _, _, body = transport.calls_of("POST")[0]
    query_call = body["methodCalls"][0]
    assert query_call[0] == "Email/query"
    return query_call[1]["filter"]


class JmapSearchSubjectQualifierCompilationTest(unittest.TestCase):
    """§S2 AC1: `subject:Amazon` compiles to a JMAP filter containing
    `{"subject": "Amazon"}` -- NOT a single `{"text": "subject:Amazon"}`. Fails
    today: `search()` sends the raw string verbatim as one `text` blob."""

    def test_subject_qualifier_compiles_to_a_dedicated_subject_filter_condition(self):
        filter_sent = _sent_filter("subject:Amazon")

        self.assertEqual(
            filter_sent.get("subject"), "Amazon",
            "the compiled filter must carry a dedicated 'subject' "
            f"FilterCondition; got {filter_sent!r}",
        )
        self.assertNotEqual(
            filter_sent, {"text": "subject:Amazon"},
            "the qualifier must NOT be sent as one opaque `text` blob "
            "containing the literal 'subject:Amazon' token",
        )


class JmapSearchAttachmentAndDateQualifierCompilationTest(unittest.TestCase):
    """§S2 AC2 (attachment + relative-date halves): `has:attachment` compiles
    to `{"hasAttachment": true}`; `newer_than:7d` compiles to
    `{"after": "<ISO-8601 cutoff>"}` at midnight UTC of the resolved absolute
    date. Fails today: both qualifiers are swallowed into the single `text`
    blob and have zero effect on the request."""

    def test_has_attachment_qualifier_compiles_to_has_attachment_true(self):
        filter_sent = _sent_filter("has:attachment")

        self.assertEqual(
            filter_sent, {"hasAttachment": True},
            f"expected a bare hasAttachment condition, got {filter_sent!r}",
        )

    def test_newer_than_seven_days_compiles_to_an_iso8601_after_cutoff(self):
        expected_cutoff_date = date.today() - timedelta(days=7)
        expected_after = f"{expected_cutoff_date.isoformat()}T00:00:00Z"

        filter_sent = _sent_filter("newer_than:7d")

        self.assertEqual(
            filter_sent, {"after": expected_after},
            f"expected an 'after' cutoff of {expected_after!r}, got "
            f"{filter_sent!r} -- newer_than: must no longer be a silent "
            "no-op swallowed into a `text` blob",
        )


class JmapSearchOperatorCompilationTest(unittest.TestCase):
    """§S2 AC2 (operator half): `a OR b` compiles to
    `{"operator": "OR", "conditions": [...]}`; implicit-AND (`a b`, no `OR`
    keyword) compiles to `{"operator": "AND", "conditions": [...]}` -- and bare
    terms still map to `{"text": <term>}` conditions inside that list. Fails
    today: both queries collapse into one `{"text": "a OR b"}` /
    `{"text": "a b"}` blob with no operator structure at all."""

    def test_or_alternation_compiles_to_operator_or_with_text_conditions(self):
        filter_sent = _sent_filter("a OR b")

        self.assertEqual(
            filter_sent,
            {"operator": "OR", "conditions": [{"text": "a"}, {"text": "b"}]},
            f"expected an OR-operator filter over the two bare terms, got "
            f"{filter_sent!r}",
        )

    def test_implicit_and_compiles_to_operator_and_with_text_conditions(self):
        filter_sent = _sent_filter("a b")

        self.assertEqual(
            filter_sent,
            {"operator": "AND", "conditions": [{"text": "a"}, {"text": "b"}]},
            f"expected an AND-operator filter (the implicit default) over the "
            f"two bare terms, got {filter_sent!r}",
        )


if __name__ == "__main__":
    unittest.main()
