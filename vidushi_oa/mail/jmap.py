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
_SUBMISSION_CAPABILITY = "urn:ietf:params:jmap:submission"

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


def _created_id(payload, method_name) -> str:
    """Return the id of the single object created by `method_name` in a JMAP
    `methodResponses` payload."""
    for response in payload.get("methodResponses", []):
        if response[0] == method_name:
            created = response[1].get("created", {})
            for obj in created.values():
                return obj.get("id", "")
    return ""


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
        return {"server_threads", "server_side_search", "projection", "send"}

    def create_draft(self, raw_rfc822, folder="Drafts") -> str:
        """Create a draft via a single `Email/set` carrying the `$draft` keyword;
        return the created email's id.

        §S1 wires the JMAP draft-creation call; §S2's `compose()` supplies the
        structured message fields the real draft carries."""
        api_url, account_id = self._session()
        body = {
            "using": [_CORE_CAPABILITY, _MAIL_CAPABILITY],
            "methodCalls": [
                ["Email/set",
                 {"accountId": account_id,
                  "create": {"draft": {"keywords": {"$draft": True}}}},
                 "0"],
            ],
        }
        status, payload = self._transport("POST", api_url, self._auth_headers(), body)
        if status != 200:
            raise RuntimeError(f"JMAP Email/set failed: HTTP {status}")
        return _created_id(payload, "Email/set")

    def send_draft(self, draft_id) -> str:
        """Submit an existing draft via exactly one `EmailSubmission/set` whose
        `create` object references the draft's email id; return the submission id."""
        api_url, account_id = self._session()
        body = {
            "using": [_CORE_CAPABILITY, _MAIL_CAPABILITY, _SUBMISSION_CAPABILITY],
            "methodCalls": [
                ["EmailSubmission/set",
                 {"accountId": account_id,
                  "create": {"submission": {"emailId": draft_id}}},
                 "0"],
            ],
        }
        status, payload = self._transport("POST", api_url, self._auth_headers(), body)
        if status != 200:
            raise RuntimeError(f"JMAP EmailSubmission/set failed: HTTP {status}")
        return _created_id(payload, "EmailSubmission/set")

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

    def _email_get_list(self, payload) -> list:
        """Return the first `Email/get` result `list` from a JMAP response."""
        for response in payload.get("methodResponses", []):
            if response[0] == "Email/get":
                return response[1].get("list", [])
        return []

    def fetch_html_body(self, uid, folder=None) -> "str | None":
        """Fetch message `uid`'s html body as a `str`, or `None`.

        Issues one `Email/get` for `ids: [uid]` requesting the `htmlBody` structure
        and its `bodyValues`, then resolves the html string from
        `bodyValues[htmlBody[0]["partId"]]["value"]`. Returns `None` when the
        message has no html body part. Extraction-only: the body is consumed
        in-engine and never enters `Message`/the AXI mail row."""
        api_url, account_id = self._session()
        body = {
            "using": [_CORE_CAPABILITY, _MAIL_CAPABILITY],
            "methodCalls": [
                ["Email/get",
                 {"accountId": account_id,
                  "ids": [uid],
                  "properties": ["htmlBody", "bodyValues"],
                  "fetchHTMLBodyValues": True},
                 "0"],
            ],
        }
        status, payload = self._transport("POST", api_url, self._auth_headers(), body)
        if status != 200:
            raise RuntimeError(f"JMAP Email/get failed: HTTP {status}")
        items = self._email_get_list(payload)
        if not items:
            return None
        html_body = items[0].get("htmlBody") or []
        if not html_body:
            return None
        part_id = html_body[0].get("partId")
        value = (items[0].get("bodyValues") or {}).get(part_id)
        if not value:
            return None
        return value.get("value")

    def fetch_message(self, uid, folder=None):
        """Fetch a single `Message` for `uid` via one `Email/get`.

        Requests the bounded header projection (`_EMAIL_PROPERTIES`) for `ids:
        [uid]` and builds a `Message` from the result (mirrors `search()`'s
        `_build_message`). Returns `None` when the id is not found."""
        api_url, account_id = self._session()
        body = {
            "using": [_CORE_CAPABILITY, _MAIL_CAPABILITY],
            "methodCalls": [
                ["Email/get",
                 {"accountId": account_id,
                  "ids": [uid],
                  "properties": list(_EMAIL_PROPERTIES)},
                 "0"],
            ],
        }
        status, payload = self._transport("POST", api_url, self._auth_headers(), body)
        if status != 200:
            raise RuntimeError(f"JMAP Email/get failed: HTTP {status}")
        items = self._email_get_list(payload)
        return self._build_message(items[0]) if items else None

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
