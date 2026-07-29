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
from vidushi_oa.mail.imap import ImapAdapter, imap_endpoint_kwargs
from vidushi_oa.mail.query import QueryModel, QueryNode, parse

_MAIL_CAPABILITY = "urn:ietf:params:jmap:mail"
_CORE_CAPABILITY = "urn:ietf:params:jmap:core"
_SUBMISSION_CAPABILITY = "urn:ietf:params:jmap:submission"

_DRAFTS_ROLE = "drafts"
_SENT_ROLE = "sent"

# The conformant delivered-to projection: `deliveredTo` is NOT an RFC 8621
# `Email` property, and a compliant server rejects the whole projection with a
# method-level `invalidArguments` error. The masked-alias correlation key is
# retained via this header projection instead (CR-OA-030 §S1).
_DELIVERED_TO_HEADER = "header:Delivered-To:asText:all"

# Bounded projection — headers/envelope only, never full body or attachments.
_EMAIL_PROPERTIES = [
    "id",
    "threadId",
    "messageId",
    "subject",
    "from",
    "to",
    "receivedAt",
    _DELIVERED_TO_HEADER,
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
    _raise_for_method_error(payload, method_name)
    for response in payload.get("methodResponses", []):
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
    _raise_for_method_error(payload, method_name)
    for response in payload.get("methodResponses", []):
        if response[0] == method_name:
            return response[1].get("ids") or []
    raise RuntimeError(f"JMAP {method_name} returned no {method_name} response")


def _call_id(response) -> str:
    """The client-supplied call id of one JMAP `methodResponses` entry.

    RFC 8620 §3.2 makes it the third element; a server that answers a malformed
    two-element response yields `""`, which matches no call and so is never
    mistaken for a specific method's failure."""
    return response[2] if len(response) > 2 else ""


def _raise_for_method_error(payload, context, call_id=None) -> None:
    """Raise for the first method-level error in a JMAP `methodResponses` payload.

    THE single method-level error check of this module (CR-OA-030 §S2). JMAP
    reports a whole-call failure INSIDE an HTTP 200 as
    ``["error", {"type": ..., "description": ...}, callId]`` — and when that call
    was the target of a back-reference, the referring call never runs at all. Every
    read and write path routes through here so no path can degrade a server error
    into an empty-but-successful-looking result.

    `context` names the failing operation in the message; the server's whole error
    object (its `type` AND `description`) is rendered verbatim so the caller sees
    what the server actually said. `call_id` restricts the check to one call of a
    batched request — the callers for which only one half of the batch is fatal."""
    for response in payload.get("methodResponses", []):
        if response[0] == "error" and (
                call_id is None or _call_id(response) == call_id):
            detail = response[1] if len(response) > 1 else {}
            raise RuntimeError(f"JMAP {context} failed: {json.dumps(detail)}")


def _first_from_address(emails) -> str:
    """The `From` address of the first email in an `Email/get` result list."""
    for item in emails or []:
        for address in item.get("from") or []:
            email = (address.get("email") or "").strip()
            if email:
                return email
    return ""


def _authorized_identity_id(identities, from_address) -> str:
    """The id of the identity authorized to send from `from_address` (RFC 8621 §6).

    An exact address match wins. A wildcard identity (`*@domain` — the form a
    provider advertises for a catch-all/masked-alias domain) matches any local
    part in its own domain. Anything else — no match, or a `From` that could not
    be read back — falls back to the first identity, which is what a
    single-identity account resolves to either way."""
    if not identities:
        return ""
    wanted = (from_address or "").strip().lower()
    if wanted:
        for identity in identities:
            if (identity.get("email") or "").strip().lower() == wanted:
                return identity["id"]
        domain = wanted.rpartition("@")[2]
        for identity in identities:
            email = (identity.get("email") or "").strip().lower()
            if domain and email.startswith("*@") and email[2:] == domain:
                return identity["id"]
    return identities[0]["id"]


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


def _header_all_to_str(value) -> str:
    """Collapse an RFC 8621 `:all` header value to a single `str`.

    Per RFC 8621 §4.1.4 the ``:all`` suffix returns a JSON **array** of strings —
    one entry per header field of that name — while a server projecting a single
    field (or a non-conformant one) may hand back a bare string. Accept both:
    a list/tuple yields its first non-blank entry, a string yields itself, and
    an absent/`null`/empty/all-blank value yields ``""``. The return is always a
    `str`, never a list and never `None`."""
    if isinstance(value, (list, tuple)):
        for entry in value:
            text = str(entry).strip() if entry is not None else ""
            if text:
                return text
        return ""
    if value is None:
        return ""
    return str(value).strip()


def compile_filter(model: QueryModel) -> dict:
    """Compile a portable `QueryModel` into an RFC 8621 `Email/query` filter
    (CR-OA-031 §S2).

    Each element of the model becomes its own `FilterCondition` — bare terms
    (and quoted phrases) map to `text`, `subject:`/`from:`/`to:` to the
    same-named conditions, `has:attachment` to `hasAttachment: true`, and
    `newer_than:` to `after` — instead of the whole query string being posted as
    one opaque `text` blob, which made every qualifier either match nothing
    (`subject:Amazon` as literal text) or silently do nothing (`newer_than:`).

    The compiler **recurses over the model's tree** (`QueryModel.root`), so a
    parenthesised group becomes a NESTED `FilterOperator`:
    `(a OR b) c` → `{"operator": "AND", "conditions": [{"operator": "OR",
    "conditions": [{"text": "a"}, {"text": "b"}]}, {"text": "c"}]}`.

    A single condition is sent bare; two or more are wrapped in the node's
    `FilterOperator` (`AND` — the implicit default — or `OR`). `newer_than:`
    resolves to the ISO-8601 `UTCDate` JMAP requires, at MIDNIGHT UTC of the
    cutoff date the parser already computed, so "newer than 7 days" includes the
    whole cutoff day — the intuitive reading of the parser's date-only model.

    `category:` has no JMAP equivalent and is deliberately NOT compiled here:
    the refusal contract that keeps it from being silently dropped is §S5's, and
    building it in this cycle would pre-empt that step.
    """
    return _compile_node(model.root)


def _compile_node(node: QueryNode) -> dict:
    """Compile one query-tree node into a JMAP filter object.

    A group recurses into its children (dropping any that compile to nothing,
    such as `category:`); a bare/single condition is returned unwrapped.
    """
    if node.is_group:
        conditions = [c for c in (_compile_node(child) for child in node.children) if c]
        if not conditions:
            return {}
        if len(conditions) == 1:
            return conditions[0]
        return {"operator": node.operator, "conditions": conditions}

    if node.term is not None:
        return {"text": node.term}
    if node.qualifier == "subject":
        return {"subject": node.value}
    if node.qualifier == "from_":
        return {"from": node.value}
    if node.qualifier == "to":
        return {"to": node.value}
    if node.qualifier == "has_attachment":
        return {"hasAttachment": True}
    if node.qualifier == "newer_than":
        return {"after": f"{node.value.isoformat()}T00:00:00Z"}
    return {}


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

    def create_draft(self, raw_rfc822) -> str:
        """Create a draft from the composed `raw_rfc822` message; return the
        created email's id.

        Takes no `folder`, unlike the IMAP contract: JMAP resolves the target
        mailbox by its `drafts` role (`_import_draft`), so a folder NAME has
        nothing to bind to and would be silently discarded.

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

    def _identity_id(self, api_url, account_id, draft_id) -> str:
        """Resolve the sending identity id AUTHORIZED for `draft_id`'s own `From`
        address; empty string when the account advertises no identity at all.

        `EmailSubmission/set` requires `identityId` (RFC 8621 §7.1): a
        spec-compliant server (Stalwart) rejects a submission that carries only
        `emailId` with `invalidProperties [emailId, identityId]`. Fastmail is
        lenient and assigns a default identity, which is why the fakes-based suite
        never caught the omission.

        The identity must also AUTHORIZE the message's `From` or the submission is
        refused with `forbiddenFrom` (RFC 8621 §7.5), so "the first identity in the
        list" is not good enough: `mail-draft --from <alias>` is a first-class
        supported flow (`mail-auth --alias`) and `Identity/get` returns identities
        in no guaranteed order, so even a plain primary-address send could pick an
        alias identity. `ids: null` returns every identity and the draft's own
        `From` is read back in the SAME round trip (one batched
        `Identity/get` + `Email/get`), then matched by `_authorized_identity_id`.

        Only the `Identity/get` call's own outcome is fatal: an `Email/get` that
        errors leaves the `From` unknown, which degrades to the first identity —
        the pre-existing behaviour — rather than blocking a send that a
        single-identity account would have completed."""
        body = {
            "using": [_CORE_CAPABILITY, _MAIL_CAPABILITY, _SUBMISSION_CAPABILITY],
            "methodCalls": [
                ["Identity/get", {"accountId": account_id, "ids": None}, "0"],
                ["Email/get",
                 {"accountId": account_id, "ids": [draft_id],
                  "properties": ["from"]},
                 "1"],
            ],
        }
        status, payload = self._transport("POST", api_url, self._auth_headers(), body)
        if status != 200:
            raise RuntimeError(f"JMAP Identity/get failed: HTTP {status}")
        _raise_for_method_error(payload, "Identity/get", call_id="0")
        identities = []
        for response in payload.get("methodResponses", []):
            if response[0] == "Identity/get":
                identities = [item for item in (response[1].get("list") or [])
                              if item.get("id")]
        return _authorized_identity_id(
            identities, _first_from_address(self._email_get_list(payload)))

    def send_draft(self, draft_id) -> str:
        """Submit an existing draft via exactly one `EmailSubmission/set` whose
        `create` object references the draft's email id and the `identityId`
        authorized for the draft's own `From` (RFC 8621 §7.1 — `_identity_id` runs
        first); return the submission id.

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
        identity_id = self._identity_id(api_url, account_id, draft_id)
        update = {"keywords/$draft": None}
        try:
            sent_id = self._mailbox_id(api_url, account_id, _SENT_ROLE)
        except (RuntimeError, OSError, ValueError):
            sent_id = ""
        if sent_id:
            update["mailboxIds"] = {sent_id: True}
        create = {"emailId": draft_id}
        if identity_id:
            create["identityId"] = identity_id
        body = {
            "using": [_CORE_CAPABILITY, _MAIL_CAPABILITY, _SUBMISSION_CAPABILITY],
            "methodCalls": [
                ["EmailSubmission/set",
                 {"accountId": account_id,
                  "create": {"submission": create},
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
                 {"accountId": account_id,
                  "filter": compile_filter(parse(query))},
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
        """Pull the `Email/get` result list out of `methodResponses` into Messages.

        A search result is only trustworthy when the server actually answered the
        back-referenced `Email/get` (CR-OA-030 §S2). A method-level
        ``["error", ...]`` — which also stops the back-reference from ever running
        — is raised by the shared `_raise_for_method_error`, and a payload with no
        `Email/get` response at all is raised naming what is missing. Only a real
        `Email/get` result yields a list, so a genuinely empty search (`ids: []`
        with a matching empty `Email/get`) stays a clean `[]` and is no longer
        indistinguishable from a failure."""
        _raise_for_method_error(payload, "search")
        for response in payload.get("methodResponses", []):
            if response[0] == "Email/get":
                return [self._build_message(item)
                        for item in response[1].get("list", [])]
        raise RuntimeError(
            "JMAP search returned no Email/get response — the back-referenced "
            "Email/get never ran, so the result is unknown, not empty")

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
            uid=item.get("id"),
        )
        message.delivered_to = _header_all_to_str(item.get(_DELIVERED_TO_HEADER))
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
    """Select a Fastmail adapter: JMAP when a token is present, else IMAP fallback.

    An optional ``config["endpoint"]`` mapping (any of ``jmap_url`` / ``imap_host`` /
    ``imap_port`` / ``smtp_host`` / ``smtp_port``) points the adapter at a local
    emulator; every value defaults to the real Fastmail provider when absent, so a
    real account behaves exactly as before."""
    endpoint = config.get("endpoint") or {}
    token = config.get("jmap_token")
    if token:
        # `session_url` is supplied only when overridden, so the absent case keeps
        # JmapAdapter's real Fastmail session default.
        jmap_kwargs = {"session_url": endpoint["jmap_url"]} if endpoint.get(
            "jmap_url") else {}
        return JmapAdapter(account, source_tag, token, transport=transport,
                           **jmap_kwargs)
    host, imap_kwargs = imap_endpoint_kwargs(endpoint, "imap.fastmail.com")
    return ImapAdapter(
        account=account,
        source_tag=source_tag,
        host=host,
        user=config.get("username", account),
        password=config["app_password"],
        conn_factory=conn_factory,
        **imap_kwargs,
    )
