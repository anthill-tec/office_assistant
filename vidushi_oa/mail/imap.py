"""IMAP mail adapters — Gmail (`X-GM-RAW`) + Yahoo (RFC 3501) (CR-OA-020 §S2).

`ImapAdapter` is a concrete `MailAdapter` over the stdlib `imaplib`/`email`
modules. It lazily opens a single IMAP connection through an injected
``conn_factory(host, port)`` (defaulting to `imaplib.IMAP4_SSL`), logs in, selects
``INBOX``, and reuses that one connection across every operation.

`GmailImapAdapter` searches with Gmail's server-side `X-GM-RAW` extension and reads
thread ids straight from `X-GM-THRID`. `YahooImapAdapter` issues a plain RFC 3501
`SEARCH` and reconstructs threads client-side from `References`/`In-Reply-To`.
"""
import email
import email.utils
import imaplib
import re
import smtplib
import sys

from vidushi_oa.mail.base import MailAdapter, Message

# Header fields requested from the server for every message.
_HEADER_FIELDS = "SUBJECT FROM TO DATE MESSAGE-ID REFERENCES IN-REPLY-TO"
_HEADER_SPEC = f"BODY.PEEK[HEADER.FIELDS ({_HEADER_FIELDS})]"

_UID_RE = re.compile(rb"UID (\d+)")
_THRID_RE = re.compile(rb"X-GM-THRID (\d+)")
_APPENDUID_RE = re.compile(rb"APPENDUID (\d+) (\d+)")
# RFC 6154 special-use attribute marking the mailbox sent mail is filed in.
_SENT_ATTR_RE = re.compile(rb"\\Sent\b", re.IGNORECASE)
# An RFC 3501 LIST line: `(attrs) delimiter name`, where the delimiter is a quoted
# char or NIL and the name is either a quoted string or a bare atom.
_LIST_LINE_RE = re.compile(rb'^\s*\([^)]*\)\s+(?:"[^"]*"|NIL)\s+(?P<name>.+?)\s*$')

# SMTP submission (STARTTLS) port for every provider's message-submission agent.
_SMTP_SUBMISSION_PORT = 587


class ImapAdapter(MailAdapter):
    """Concrete IMAP adapter with a lazily-created, reused connection."""

    # Whether the provider files its own copy of an SMTP submission into Sent.
    # Gmail does (overridden below); Fastmail Basic and Yahoo do not, so this
    # adapter APPENDs the copy itself.
    server_files_sent_copy = False

    def __init__(self, account, source_tag, host, user, password, port=993,
                 conn_factory=None, smtp_host=None, smtp_port=_SMTP_SUBMISSION_PORT):
        self.account = account
        self.source_tag = source_tag
        self.host = host
        self.user = user
        self.password = password
        self.port = port
        self._factory = conn_factory or (lambda h, p: imaplib.IMAP4_SSL(h, p))
        self._connection = None
        self._sent_mailbox = None
        # Derive the SMTP submission host from the IMAP host when not injected
        # (imap.gmail.com -> smtp.gmail.com, imap.mail.yahoo.com -> smtp.mail.yahoo.com).
        self.smtp_host = smtp_host or (
            "smtp." + host[len("imap."):] if host.startswith("imap.") else host)
        self.smtp_port = smtp_port

    def _conn(self):
        """Create the connection once, then return the cached one."""
        if self._connection is None:
            conn = self._factory(self.host, self.port)
            conn.login(self.user, self.password)
            conn.select("INBOX")
            self._connection = conn
        return self._connection

    def capabilities(self) -> set:
        return set()

    def search(self, query, folder=None, limit=None) -> list:
        conn = self._conn()
        typ, data = conn.uid("SEARCH", query)
        uids = _parse_uids(data)
        if limit is not None:
            uids = uids[:limit]
        return self._fetch(uids)

    def fetch_message(self, uid, folder=None):
        messages = self._fetch([str(uid)])
        return messages[0] if messages else None

    def list_folders(self) -> list:
        conn = self._conn()
        typ, data = conn.list()
        return _parse_folders(data)

    def create_draft(self, raw_rfc822, folder="Drafts") -> str:
        """APPEND `raw_rfc822` to `folder` flagged `\\Draft`; return a draft id.

        A single IMAP `APPEND` stores the pre-rendered RFC 5322 bytes as a draft;
        the returned id is the server-assigned UID parsed from the `APPENDUID`
        response code (falling back to the raw response text when absent).

        `imaplib` raises only on a tagged `BAD`, so a refusal (`NO [TRYCREATE]`,
        `NO [OVERQUOTA]`) returns quietly and would otherwise be reported as a
        successful draft whose id is the server's error text. The tagged status is
        checked and a rejection raised structurally, mirroring the JMAP path."""
        conn = self._conn()
        typ, data = conn.append(folder, r"(\Draft)", None, raw_rfc822)
        if typ != "OK":
            raise RuntimeError(
                f"IMAP APPEND to {folder!r} rejected: {typ} {_response_text(data)}")
        return _parse_append_uid(data)

    def send_draft(self, draft_id, folder="Drafts") -> str:
        """Dispatch the stored draft `draft_id` over SMTP (STARTTLS submission).

        Fetches the drafted message's raw RFC 5322 bytes from `folder` by its
        UID, parses the envelope sender (its `From`) and the recipient list (its
        `To` + `Cc`), then connects to the provider's submission host on :587,
        upgrades with STARTTLS, authenticates with this adapter's own IMAP
        credential (the app-password already authorizes SMTP — DN §Decision 7),
        and issues exactly one `sendmail` of the REAL draft bytes to the REAL
        recipients. Returns the message's own `Message-ID` when present, else a
        freshly-minted one.

        Once the provider has accepted the message, `_file_sent_copy` files it in
        Sent and retires the Drafts copy — the IMAP counterpart of the JMAP
        `onSuccessUpdateEmail` patch."""
        raw_bytes = self._fetch_draft_bytes(draft_id, folder)
        parsed = email.message_from_bytes(raw_bytes)
        from_addr = email.utils.parseaddr(parsed.get("From", ""))[1] or self.user
        recipient_pairs = email.utils.getaddresses(
            parsed.get_all("To", []) + parsed.get_all("Cc", []))
        recipients = [addr for _name, addr in recipient_pairs if addr]
        message_id = (parsed.get("Message-ID") or "").strip() or \
            email.utils.make_msgid(domain=self.smtp_host)
        smtp = smtplib.SMTP(self.smtp_host, self.smtp_port)
        smtp.starttls()
        smtp.login(self.user, self.password)
        smtp.sendmail(from_addr, recipients, raw_bytes)
        self._file_sent_copy(raw_bytes, draft_id, folder)
        return message_id

    def _file_sent_copy(self, raw_bytes, draft_id, folder) -> None:
        """After a successful submission, APPEND the sent bytes to the `\\Sent`
        mailbox and stop the Drafts copy from being a draft.

        Parity with the JMAP `onSuccessUpdateEmail` patch: without this the sent
        message lingers in Drafts flagged `\\Draft` and Sent holds no record of the
        correspondence. Providers that file their own copy of an SMTP submission
        (Gmail) skip the APPEND so the user's Sent does not gain a duplicate.

        This is bookkeeping that runs AFTER delivery, so every step is best-effort:
        the message is already in the provider's hands, and reporting a mailbox
        failure here would tell the user nothing was sent.

        The Drafts copy is destroyed ONLY once a Sent copy is confirmed to exist —
        a confirmed-`OK` APPEND, or a provider that files its own. `imaplib` returns
        a tagged `NO` (quota, permission, mailbox vanished between LIST and APPEND)
        quietly rather than raising, so every tagged status on the path is read: on
        any failure, or when the account advertises no Sent mailbox, the draft is
        left exactly where it is so the message can never end up in neither
        folder."""
        try:
            conn = self._conn()
            if not self.server_files_sent_copy:
                sent = self._sent_mailbox_name()
                if not sent:
                    _warn(f"no \\Sent mailbox found; the sent message stays in {folder!r}")
                    return
                typ, data = conn.append(sent, r"(\Seen)", None, raw_bytes)
                if typ != "OK":
                    _warn(f"APPEND to {sent!r} rejected ({typ} {_response_text(data)}); "
                          f"the sent message stays in {folder!r}")
                    return
            conn.select(folder)
            conn.uid("STORE", str(draft_id), "-FLAGS", r"(\Draft)")
            typ, data = conn.uid("STORE", str(draft_id), "+FLAGS", r"(\Deleted)")
            if typ != "OK":
                _warn(f"could not retire the {folder!r} copy of draft {draft_id} "
                      f"({typ} {_response_text(data)})")
                return
            # UID EXPUNGE removes only this draft; a bare EXPUNGE would also reap
            # anything else the user had flagged `\Deleted` in the folder.
            conn.uid("EXPUNGE", str(draft_id))
        except (imaplib.IMAP4.error, OSError, ValueError) as e:
            _warn(f"mailbox bookkeeping after a delivered send failed: {e}")
            return

    def _sent_mailbox_name(self) -> str:
        """Resolve (and cache) the `\\Sent` special-use mailbox name (RFC 6154);
        empty string when the account advertises none."""
        if self._sent_mailbox is None:
            typ, data = self._conn().list()
            self._sent_mailbox = _find_sent_mailbox(data)
        return self._sent_mailbox

    def fetch_html_body(self, uid, folder=None) -> "str | None":
        """Fetch message `uid`'s decoded `text/html` part as a `str`, or `None`.

        Selects `folder` (default `INBOX`), issues one UID `FETCH (BODY[])` — the
        CR-022 `_fetch_draft_bytes` tuple shape — parses the raw RFC 5322 bytes and
        walks the MIME tree for the first `text/html` part, decoding it with that
        part's charset. Returns `None` when the message carries no html part (e.g.
        a plain-text-only message); the body is consumed in-engine only and is
        never surfaced in `Message`/the AXI mail row."""
        conn = self._conn()
        conn.select(folder or "INBOX")
        typ, data = conn.uid("FETCH", str(uid), "(BODY[])")
        raw = None
        for item in data or []:
            if isinstance(item, tuple) and len(item) == 2:
                raw = item[1]
                break
        if raw is None:
            return None
        parsed = email.message_from_bytes(raw)
        for part in parsed.walk():
            if part.get_content_type() != "text/html":
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
        return None

    def _fetch_draft_bytes(self, draft_id, folder="Drafts") -> bytes:
        """Fetch the raw RFC 5322 bytes of draft `draft_id` from `folder` by UID."""
        conn = self._conn()
        conn.select(folder)
        typ, data = conn.uid("FETCH", str(draft_id), "(BODY[])")
        for item in data or []:
            if isinstance(item, tuple) and len(item) == 2:
                return item[1]
        raise ValueError(f"draft {draft_id!r} not found in folder {folder!r}")

    def _fetch_spec(self) -> str:
        """The FETCH item spec — subclasses extend it (e.g. with `X-GM-THRID`)."""
        return f"({_HEADER_SPEC})"

    def _fetch(self, uids) -> list:
        if not uids:
            return []
        conn = self._conn()
        typ, data = conn.uid("FETCH", ",".join(uids), self._fetch_spec())
        return self._parse_fetch(data)

    def _parse_fetch(self, data) -> list:
        messages = []
        for item in data or []:
            if not isinstance(item, tuple) or len(item) != 2:
                continue
            descriptor, header_bytes = item
            messages.append(self._build_message(descriptor, header_bytes))
        return self._post_process(messages)

    def _build_message(self, descriptor, header_bytes) -> Message:
        parsed = email.message_from_bytes(header_bytes)
        uid_match = _UID_RE.search(descriptor)
        message = Message(
            id=(parsed.get("Message-ID") or "").strip(),
            account=self.account,
            source_tag=self.source_tag,
            subject=parsed.get("Subject", ""),
            sender=parsed.get("From", ""),
            to=parsed.get("To", ""),
            date=parsed.get("Date", ""),
            uid=uid_match.group(1).decode() if uid_match else None,
        )
        thrid_match = _THRID_RE.search(descriptor)
        if thrid_match:
            message.thread_id = thrid_match.group(1).decode()
        message.references = parsed.get("References", "")
        message.in_reply_to = parsed.get("In-Reply-To", "")
        return message

    def _post_process(self, messages) -> list:
        """Hook for subclasses to enrich messages after parsing (default: no-op)."""
        return messages


class GmailImapAdapter(ImapAdapter):
    """Gmail adapter — server-side `X-GM-RAW` search and `X-GM-THRID` threads."""

    # Gmail files every SMTP submission into "Sent Mail" itself.
    server_files_sent_copy = True

    def capabilities(self) -> set:
        return {"raw_query", "server_side_categories", "server_threads", "send"}

    def search(self, query, folder=None, limit=None) -> list:
        conn = self._conn()
        escaped = query.replace("\\", "\\\\").replace('"', '\\"')
        typ, data = conn.uid("SEARCH", "X-GM-RAW", f'"{escaped}"')
        uids = _parse_uids(data)
        if limit is not None:
            uids = uids[:limit]
        return self._fetch(uids)

    def _fetch_spec(self) -> str:
        return f"(X-GM-THRID {_HEADER_SPEC})"


class YahooImapAdapter(ImapAdapter):
    """Yahoo adapter — plain RFC 3501 search + client-side thread reconstruction."""

    def capabilities(self) -> set:
        return {"send"}

    def search(self, query, folder=None, limit=None) -> list:
        conn = self._conn()
        typ, data = conn.uid("SEARCH", query)
        uids = _parse_uids(data)
        if limit is not None:
            uids = uids[:limit]
        return self._fetch(uids)

    def _post_process(self, messages) -> list:
        """Reconstruct `thread_id` from `References`/`In-Reply-To` roots."""
        for message in messages:
            root = _thread_root(message)
            message.thread_id = root
        return messages


def _parse_append_uid(data) -> str:
    """Return the APPEND-assigned UID from an IMAP `APPENDUID` response, or the
    raw response text as a fallback draft id."""
    for chunk in data or []:
        if isinstance(chunk, bytes):
            match = _APPENDUID_RE.search(chunk)
            if match:
                return match.group(2).decode()
    for chunk in data or []:
        if isinstance(chunk, bytes) and chunk.strip():
            return chunk.strip().decode(errors="replace")
    return "draft"


def _find_sent_mailbox(data) -> str:
    """The name of the `\\Sent` special-use mailbox in an IMAP LIST response."""
    for line in data or []:
        if not isinstance(line, bytes) or not _SENT_ATTR_RE.search(line):
            continue
        name = _list_mailbox_name(line)
        if name:
            return name
    return ""


def _list_mailbox_name(line) -> str:
    """The mailbox name from one IMAP LIST line, quoted or a bare atom.

    A naive `rsplit(b'"')` yields the hierarchy delimiter for an unquoted name
    (`(\\HasNoChildren \\Sent) "/" Sent`), so the attribute list and the delimiter
    are consumed explicitly and only a genuinely quoted remainder is unquoted."""
    match = _LIST_LINE_RE.match(line)
    if not match:
        return ""
    name = match.group("name")
    if len(name) >= 2 and name.startswith(b'"') and name.endswith(b'"'):
        name = name[1:-1]
    return name.decode(errors="replace")


def _response_text(data) -> str:
    """The human-readable text of a tagged IMAP response payload."""
    parts = [chunk.decode(errors="replace") for chunk in data or []
             if isinstance(chunk, bytes) and chunk.strip()]
    return " ".join(parts)


def _warn(message) -> None:
    sys.stderr.write(f"warn: {message}\n")


def _parse_uids(data) -> list:
    """Extract a UID list from an IMAP SEARCH response payload."""
    uids: list = []
    for chunk in data or []:
        if not chunk:
            continue
        if isinstance(chunk, bytes):
            uids.extend(tok.decode() for tok in chunk.split())
    return uids


def _parse_folders(data) -> list:
    """Extract folder names from an IMAP LIST response payload."""
    folders = []
    for line in data or []:
        if not isinstance(line, bytes):
            continue
        name = _list_mailbox_name(line)
        if name:
            folders.append(name)
    return folders


def _thread_root(message) -> str:
    """The root Message-ID of `message`'s thread, from References/In-Reply-To."""
    references = (getattr(message, "references", "") or "").split()
    if references:
        return references[0]
    in_reply_to = (getattr(message, "in_reply_to", "") or "").strip()
    if in_reply_to:
        return in_reply_to
    return message.id
