"""CR-OA-024 §S1 — Fastmail JMAP POST missing `Content-Type` header (RED).

`JmapAdapter._auth_headers()` (`vidushi_oa/mail/jmap.py:71-72`) currently returns
only `{"Authorization": f"Bearer {self.token}"}`. Fastmail's JMAP API 400s the POST
(`jmap.py:103`) because the request lacks `Content-Type: application/json`. This
file asserts:

  1. `_auth_headers()` returns a dict whose entries are EXACTLY
     `Authorization: Bearer <token>` and `Content-Type: application/json`.
  2. Driving `JmapAdapter` through its real public `search()` path (via an
     injected fake transport, no network) captures the headers passed to the
     `POST` at the JMAP `api_url`, and those headers include
     `Content-Type: application/json`.

Both must FAIL against current code because `_auth_headers()` omits the
`Content-Type` key entirely.
"""
import unittest

from vidushi_oa.mail.jmap import JmapAdapter

SESSION_URL = "https://api.fastmail.com/jmap/session"
API_URL = "https://api.fastmail.com/jmap/api/"
ACCOUNT_ID = "u1234567"

_CANNED_SESSION = {
    "apiUrl": API_URL,
    "accounts": {
        ACCOUNT_ID: {
            "name": "new.book1604@fastmail.com",
            "isPersonal": True,
            "accountCapabilities": {"urn:ietf:params:jmap:mail": {}},
        },
    },
    "primaryAccounts": {"urn:ietf:params:jmap:mail": ACCOUNT_ID},
    "capabilities": {"urn:ietf:params:jmap:mail": {}},
}

_CANNED_POST_RESPONSE = {
    "methodResponses": [
        ["Email/query", {"accountId": ACCOUNT_ID, "ids": []}, "0"],
        [
            "Email/get",
            {"accountId": ACCOUNT_ID, "list": [], "notFound": []},
            "1",
        ],
    ],
}


class FakeTransport:
    """Records every `(method, url, headers, body)` call and returns canned
    `(status, dict)` tuples shaped like real JMAP HTTP responses — no network."""

    def __init__(self, session_response=None, post_response=None):
        self.session_response = session_response or _CANNED_SESSION
        self.post_response = post_response or _CANNED_POST_RESPONSE
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        if method == "GET":
            return 200, self.session_response
        return 200, self.post_response

    def calls_of(self, method):
        return [c for c in self.calls if c[0] == method]


class JmapAuthHeadersContentTypeTest(unittest.TestCase):
    """§S1 AC: `_auth_headers()` returns exactly Authorization + Content-Type."""

    def test_auth_headers_includes_authorization_and_json_content_type(self):
        adapter = JmapAdapter(
            account="fastmail_main", source_tag="[FM]", token="secret-token",
            session_url=SESSION_URL, transport=FakeTransport(),
        )

        headers = adapter._auth_headers()

        self.assertEqual(
            headers,
            {
                "Authorization": "Bearer secret-token",
                "Content-Type": "application/json",
            },
        )
        self.assertEqual(headers["Authorization"], "Bearer secret-token")
        self.assertEqual(headers["Content-Type"], "application/json")


class JmapSearchPostContentTypeIntegrationTest(unittest.TestCase):
    """§S1 AC (integration): the real `search()` POST path must carry the
    Content-Type header — driven through the public API, not `_auth_headers()`
    called directly."""

    def test_search_post_to_api_url_carries_json_content_type_header(self):
        transport = FakeTransport()
        adapter = JmapAdapter(
            account="fastmail_main", source_tag="[FM]", token="secret-token",
            session_url=SESSION_URL, transport=transport,
        )

        adapter.search("invoice")

        post_calls = transport.calls_of("POST")
        self.assertEqual(len(post_calls), 1)
        method, url, headers, body = post_calls[0]
        self.assertEqual(url, API_URL)
        self.assertEqual(headers.get("Content-Type"), "application/json")
        self.assertEqual(headers.get("Authorization"), "Bearer secret-token")


if __name__ == "__main__":
    unittest.main()
