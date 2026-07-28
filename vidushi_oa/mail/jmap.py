"""Fastmail JMAP adapter (thin-HTTP) + IMAP fallback selector (CR-OA-020 §S3).

`JmapAdapter` is a concrete `MailAdapter` speaking JMAP over plain HTTP through an
injected ``transport(method, url, headers, body_dict_or_None) -> (status, dict)``
callable (defaulting to a stdlib-`urllib` transport). It fetches the JMAP session
document exactly once — caching the ``apiUrl`` and primary mail ``accountId`` — then
serves every `search()` from a single batched POST: an ``Email/query`` whose ids are
back-referenced by an ``Email/get`` (the JMAP ``#ids`` result reference) with a
bounded property projection (no message bodies/attachments).

`fastmail_adapter()` selects between JMAP (when the config carries a JMAP token) and
an IMAP fallback against ``imap.fastmail.com`` for Basic-plan app-password configs.
"""
import json
import urllib.request

from vidushi_oa.mail.base import MailAdapter, Message
from vidushi_oa.mail.imap import ImapAdapter

_MAIL_CAPABILITY = "urn:ietf:params:jmap:mail"
_CORE_CAPABILITY = "urn:ietf:params:jmap:core"

# Bounded projection — headers/envelope only, never full body or attachments.
_EMAIL_PROPERTIES = [
    "id",
    "threadId",
    "messageId",
    "subject",
    "from",
    "to",
    "receivedAt",
    "deliveredTo",
]


def _urllib_transport(method, url, headers, body):
    """Default stdlib-`urllib` transport (never exercised in tests)."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request) as response:
        status = response.getcode()
        payload = json.loads(response.read().decode("utf-8"))
    return status, payload


def _format_address(addresses):
    """Render the first JMAP `EmailAddress` as ``Name <email>`` (or ``email``)."""
    if not addresses:
        return ""
    first = addresses[0]
    name = (first.get("name") or "").strip()
    email = (first.get("email") or "").strip()
    if name and email:
        return f"{name} <{email}>"
    return email or name


class JmapAdapter(MailAdapter):
    """Concrete JMAP adapter — session cached once, one batched POST per search."""

    def __init__(self, account, source_tag, token,
                 session_url="https://api.fastmail.com/jmap/session", transport=None):
        self.account = account
        self.source_tag = source_tag
        self.token = token
        self.session_url = session_url
        self._transport = transport or _urllib_transport
        self._api_url = None
        self._account_id = None

    def _auth_headers(self):
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def _session(self):
        """Fetch + cache the session document once; reuse it thereafter."""
        if self._api_url is None:
            status, payload = self._transport(
                "GET", self.session_url, self._auth_headers(), None)
            if status != 200:
                raise RuntimeError(f"JMAP session fetch failed: HTTP {status}")
            self._api_url = payload["apiUrl"]
            self._account_id = payload["primaryAccounts"][_MAIL_CAPABILITY]
        return self._api_url, self._account_id

    def capabilities(self) -> set:
        return {"server_threads", "server_side_search", "projection"}

    def search(self, query, folder=None, limit=None) -> list:
        api_url, account_id = self._session()
        body = {
            "using": [_CORE_CAPABILITY, _MAIL_CAPABILITY],
            "methodCalls": [
                ["Email/query",
                 {"accountId": account_id, "filter": {"text": query}},
                 "0"],
                ["Email/get",
                 {"accountId": account_id,
                  "#ids": {"resultOf": "0", "name": "Email/query", "path": "/ids"},
                  "properties": list(_EMAIL_PROPERTIES)},
                 "1"],
            ],
        }
        status, payload = self._transport("POST", api_url, self._auth_headers(), body)
        if status != 200:
            raise RuntimeError(f"JMAP request failed: HTTP {status}")
        return self._parse(payload)

    def _parse(self, payload) -> list:
        """Pull the `Email/get` result list out of `methodResponses` into Messages."""
        emails = []
        for response in payload.get("methodResponses", []):
            if response[0] == "Email/get":
                emails = response[1].get("list", [])
                break
        return [self._build_message(item) for item in emails]

    def _build_message(self, item) -> Message:
        message_ids = item.get("messageId") or []
        message = Message(
            id=message_ids[0] if message_ids else "",
            account=self.account,
            source_tag=self.source_tag,
            subject=item.get("subject", ""),
            sender=_format_address(item.get("from")),
            to=_format_address(item.get("to")),
            date=item.get("receivedAt", ""),
            thread_id=item.get("threadId"),
        )
        message.delivered_to = item.get("deliveredTo", "")
        return message

    def fetch_message(self, uid, folder=None):
        raise NotImplementedError("JmapAdapter.fetch_message is not implemented")

    def list_folders(self) -> list:
        raise NotImplementedError("JmapAdapter.list_folders is not implemented")


def fastmail_adapter(account, source_tag, config, transport=None, conn_factory=None):
    """Select a Fastmail adapter: JMAP when a token is present, else IMAP fallback."""
    token = config.get("jmap_token")
    if token:
        return JmapAdapter(account, source_tag, token, transport=transport)
    return ImapAdapter(
        account=account,
        source_tag=source_tag,
        host="imap.fastmail.com",
        user=config.get("username", account),
        password=config["app_password"],
        conn_factory=conn_factory,
    )
