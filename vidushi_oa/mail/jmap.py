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

_DRAFTS_ROLE = "drafts"
_SENT_ROLE = "sent"

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
    """Default stdlib-`urllib` transport (never exercised in tests).

    A `bytes`/`bytearray` body is sent verbatim (the JMAP blob upload posts the
    literal RFC822 bytes); any other non-None body is JSON-encoded."""
    if body is None:
        data = None
    elif isinstance(body, (bytes, bytearray)):
        data = bytes(body)
    else:
        data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request) as response:
        status = response.getcode()
        payload = json.loads(response.read().decode("utf-8"))
    return status, payload


def _created_id(payload, method_name) -> str:
    """Return the id of the single object created by `method_name` in a JMAP
    `methodResponses` payload.

    JMAP reports method-level failures INSIDE an HTTP 200 response — either a
    per-object `notCreated` SetError or a whole-call `["error", {...}, callId]`
    response — so every one of those is raised as a structured `RuntimeError`
    rather than degrading into an empty id a caller would report as success."""
    for response in payload.get("methodResponses", []):
        if response[0] == "error":
            raise RuntimeError(
                f"JMAP {method_name} failed: {json.dumps(response[1])}")
        if response[0] == method_name:
            not_created = response[1].get("notCreated") or {}
            if not_created:
                raise RuntimeError(
                    f"JMAP {method_name} rejected: {json.dumps(not_created)}")
            for obj in (response[1].get("created") or {}).values():
                created_id = obj.get("id", "")
                if created_id:
                    return created_id
            raise RuntimeError(f"JMAP {method_name} returned no created id")
    raise RuntimeError(f"JMAP {method_name} returned no {method_name} response")


def _queried_ids(payload, method_name) -> list:
    """Return the `ids` list `method_name` answered with in a JMAP `methodResponses`
    payload.

    Same HTTP-200-carries-the-failure hazard as `_created_id`: a whole-call
    `["error", {...}, callId]` — or a payload carrying no `method_name` response at
    all — is raised structurally, so a server/auth failure is never mistaken for a
    query that legitimately matched nothing."""
    for response in payload.get("methodResponses", []):
        if response[0] == "error":
            raise RuntimeError(
                f"JMAP {method_name} failed: {json.dumps(response[1])}")
        if response[0] == method_name:
            return response[1].get("ids") or []
    raise RuntimeError(f"JMAP {method_name} returned no {method_name} response")


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
        self._upload_url = None
        self._mailbox_ids = {}

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
            upload_url = payload.get("uploadUrl")
            if upload_url and "{accountId}" in upload_url:
                upload_url = upload_url.replace("{accountId}", self._account_id)
            self._upload_url = upload_url
        return self._api_url, self._account_id

    def capabilities(self) -> set:
        return {"server_threads", "server_side_search", "projection", "send"}

    def create_draft(self, raw_rfc822, folder="Drafts") -> str:
        """Create a draft from the composed `raw_rfc822` message; return the
        created email's id.

        The literal RFC822 bytes are uploaded as a JMAP blob and then imported
        into the Drafts mailbox with the `$draft` keyword — so the composed
        content actually reaches the server. `uploadUrl` is a mandatory Session
        property (RFC 8620 §2); a session without one offers no way to transmit
        the content, so it is a structured failure rather than a silent
        content-less draft."""
        api_url, account_id = self._session()
        if not self._upload_url:
            raise RuntimeError(
                "JMAP session advertises no uploadUrl — cannot create a draft "
                "carrying the composed message")
        blob_id = self._upload_blob(self._upload_url, raw_rfc822)
        return self._import_draft(api_url, account_id, blob_id)

    def _upload_blob(self, upload_url, raw_rfc822) -> str:
        """Upload the literal `raw_rfc822` bytes to the session `uploadUrl` and
        return the resulting `blobId`.

        Any 2xx is a successful upload — RFC 8620 §6.1 does not mandate 200, and
        Fastmail/Cyrus answers `201 Created`. The message is encoded first: a
        transport sends only a `bytes` body verbatim, so a `str` would go up
        JSON-quoted and produce a corrupt draft nothing downstream can detect."""
        if isinstance(raw_rfc822, str):
            raw_rfc822 = raw_rfc822.encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "message/rfc822",
        }
        status, payload = self._transport("POST", upload_url, headers, raw_rfc822)
        if not 200 <= status < 300:
            raise RuntimeError(f"JMAP blob upload failed: HTTP {status}")
        blob_id = payload.get("blobId", "")
        if not blob_id:
            raise RuntimeError("JMAP blob upload returned no blobId")
        return blob_id

    def _import_draft(self, api_url, account_id, blob_id) -> str:
        """Import an uploaded blob into the Drafts mailbox as a `$draft` message
        via one `Email/import`; return the created email's id.

        RFC 8621 §4.8 requires at least one mailbox on an `EmailImport`, so an
        unresolvable Drafts mailbox fails fast instead of posting a
        guaranteed-invalid `mailboxIds: {}` the user would see as a saved draft.

        Every `Email/import` creates a new draft: `compose()` stamps a fresh
        `Message-ID` and `Date` on each call, so a redraft never serialises to the
        same bytes and never collides with the content-addressed blob of an earlier
        one. Re-running `mail-draft` therefore yields a second draft rather than
        resolving back to the first."""
        drafts_id = self._mailbox_id(api_url, account_id, _DRAFTS_ROLE)
        if not drafts_id:
            raise RuntimeError(
                "JMAP account has no Drafts mailbox — refusing to import a draft "
                "with no mailbox")
        mailbox_ids = {drafts_id: True}
        body = {
            "using": [_CORE_CAPABILITY, _MAIL_CAPABILITY],
            "methodCalls": [
                ["Email/import",
                 {"accountId": account_id,
                  "emails": {"draft": {"blobId": blob_id,
                                       "mailboxIds": mailbox_ids,
                                       "keywords": {"$draft": True}}}},
                 "0"],
            ],
        }
        status, payload = self._transport("POST", api_url, self._auth_headers(), body)
        if status != 200:
            raise RuntimeError(f"JMAP Email/import failed: HTTP {status}")
        return _created_id(payload, "Email/import")

    def _mailbox_id(self, api_url, account_id, role) -> str:
        """Resolve (and cache) the id of the mailbox carrying `role` via one
        `Mailbox/query`; empty string when the account genuinely has no such mailbox.

        A method-level failure arrives inside an HTTP 200, so the query's own outcome
        is inspected and raised verbatim — otherwise an auth/account error degrades
        into the flatly wrong "your account has no Drafts mailbox" diagnosis. Only a
        query that actually answered is cached, so a failed one is retried rather than
        repeating the same wrong verdict for the life of the process."""
        if role not in self._mailbox_ids:
            body = {
                "using": [_CORE_CAPABILITY, _MAIL_CAPABILITY],
                "methodCalls": [
                    ["Mailbox/query",
                     {"accountId": account_id, "filter": {"role": role}},
                     "0"],
                ],
            }
            status, payload = self._transport(
                "POST", api_url, self._auth_headers(), body)
            if status != 200:
                raise RuntimeError(f"JMAP Mailbox/query failed: HTTP {status}")
            ids = _queried_ids(payload, "Mailbox/query")
            self._mailbox_ids[role] = ids[0] if ids else ""
        return self._mailbox_ids[role]

    def send_draft(self, draft_id) -> str:
        """Submit an existing draft via exactly one `EmailSubmission/set` whose
        `create` object references the draft's email id; return the submission id.

        The submission carries an `onSuccessUpdateEmail` patch so a sent message
        stops being a draft: the `$draft` keyword is cleared and, when the account
        has a `sent`-role mailbox, the email is moved into it. Without the patch the
        message would sit in Drafts flagged `$draft` forever and Sent would hold no
        record of the correspondence. A submission needs no mailbox, so nothing about
        the Sent lookup is fatal here: an account with no Sent mailbox — or one whose
        `Mailbox/query` fails outright — still sends, and only the move is skipped.
        That guard spans the lookup's WHOLE live failure surface, not just the
        method-level `RuntimeError`: `urlopen` raises `HTTPError` (an `OSError`) on
        every 4xx/5xx, and a 2xx carrying a captive-portal page raises `ValueError`
        from the transport's `json.loads`. `_session()` is already resolved by then,
        so nothing beyond the Sent lookup itself is swallowed."""
        api_url, account_id = self._session()
        update = {"keywords/$draft": None}
        try:
            sent_id = self._mailbox_id(api_url, account_id, _SENT_ROLE)
        except (RuntimeError, OSError, ValueError):
            sent_id = ""
        if sent_id:
            update["mailboxIds"] = {sent_id: True}
        body = {
            "using": [_CORE_CAPABILITY, _MAIL_CAPABILITY, _SUBMISSION_CAPABILITY],
            "methodCalls": [
                ["EmailSubmission/set",
                 {"accountId": account_id,
                  "create": {"submission": {"emailId": draft_id}},
                  "onSuccessUpdateEmail": {"#submission": update}},
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
