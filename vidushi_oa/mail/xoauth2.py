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
import urllib.error
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
    """Exchange a refresh token for a fresh access token, returned as a str.

    A revoked/expired refresh token surfaces as a single catchable `LookupError`
    — whether the transport raises `HTTPError` (a 4xx `invalid_grant`) or returns
    an OAuth error body with no `access_token` field — so the lazy `_conn()`
    refresh renders as a structured error, never a raw traceback.
    """
    transport = transport or _urllib_transport
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    })
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    try:
        _status, payload = transport("POST", token_url, headers, body)
        return payload["access_token"]
    except (KeyError, urllib.error.HTTPError) as e:
        raise LookupError(
            "Gmail XOAUTH2 token refresh failed (revoked or expired refresh "
            "token); re-run `voa mail-auth` to re-authorize"
        ) from e


class GmailXoauth2Adapter(GmailImapAdapter):
    """Gmail adapter authenticating via `XOAUTH2` instead of a password.

    `access_token` is either a ready string OR a zero-argument callable that
    mints one lazily (a token provider). The provider is invoked at most once,
    on first `_conn()` — never at construction — so building the adapter (e.g.
    from `mail-accounts` or for an unrelated account) performs no network I/O.
    """

    def __init__(self, account, source_tag, host, user, access_token, port=993,
                 conn_factory=None):
        super().__init__(account, source_tag, host, user, password="",
                         port=port, conn_factory=conn_factory)
        self.access_token = access_token
        self._resolved_token = None

    def _token(self):
        """Resolve (and cache) the access token — invoking the provider once."""
        if self._resolved_token is None:
            token = self.access_token
            self._resolved_token = token() if callable(token) else token
        return self._resolved_token

    def _conn(self):
        """Create + XOAUTH2-authenticate the connection once, then reuse it."""
        if self._connection is None:
            token = self._token()
            conn = self._factory(self.host, self.port)
            conn.authenticate(
                "XOAUTH2",
                lambda _challenge: _xoauth2_raw(self.user, token),
            )
            conn.select("INBOX")
            self._connection = conn
        return self._connection
