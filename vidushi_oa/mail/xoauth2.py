"""Gmail Workspace XOAUTH2 IMAP fallback (CR-OA-020 §S6).

Some Google Workspace tenants disable app passwords, so the password-based
`GmailImapAdapter` login cannot be used. `GmailXoauth2Adapter` authenticates
with the IMAP `XOAUTH2` SASL mechanism instead, driven by a short-lived OAuth
access token that `refresh_access_token` mints from a refresh token.

Stdlib only — `base64` for the SASL string and `urllib`/`json` for the default
token transport. No `httpx`, no Google client libraries.
"""
import base64
import json
import urllib.parse
import urllib.request

from vidushi_oa.mail.imap import GmailImapAdapter

_TOKEN_URL = "https://oauth2.googleapis.com/token"


def _xoauth2_raw(user, access_token) -> bytes:
    """The RAW (decoded) `XOAUTH2` SASL bytes for `user` + `access_token`."""
    return b"user=%s\x01auth=Bearer %s\x01\x01" % (
        user.encode(),
        access_token.encode(),
    )


def build_xoauth2_string(user, access_token) -> bytes:
    """The canonical base64-encoded `XOAUTH2` SASL string."""
    return base64.b64encode(_xoauth2_raw(user, access_token))


def _urllib_transport(method, url, headers, body):
    """Default stdlib `transport(method, url, headers, body) -> (status, dict)`."""
    data = body.encode() if isinstance(body, str) else body
    request = urllib.request.Request(url, data=data, headers=headers,
                                     method=method)
    with urllib.request.urlopen(request) as response:
        payload = json.loads(response.read().decode())
        return response.status, payload


def refresh_access_token(client_id, client_secret, refresh_token,
                         transport=None, token_url=_TOKEN_URL) -> str:
    """Exchange a refresh token for a fresh access token, returned as a str."""
    transport = transport or _urllib_transport
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    })
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    _status, payload = transport("POST", token_url, headers, body)
    return payload["access_token"]


class GmailXoauth2Adapter(GmailImapAdapter):
    """Gmail adapter authenticating via `XOAUTH2` instead of a password."""

    def __init__(self, account, source_tag, host, user, access_token, port=993,
                 conn_factory=None):
        super().__init__(account, source_tag, host, user, password="",
                         port=port, conn_factory=conn_factory)
        self.access_token = access_token

    def _conn(self):
        """Create + XOAUTH2-authenticate the connection once, then reuse it."""
        if self._connection is None:
            conn = self._factory(self.host, self.port)
            conn.authenticate(
                "XOAUTH2",
                lambda _challenge: _xoauth2_raw(self.user, self.access_token),
            )
            conn.select("INBOX")
            self._connection = conn
        return self._connection
