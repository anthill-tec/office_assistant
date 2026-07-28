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
import imaplib
import re

from vidushi_oa.mail.base import MailAdapter, Message

# Header fields requested from the server for every message.
_HEADER_FIELDS = "SUBJECT FROM TO DATE MESSAGE-ID REFERENCES IN-REPLY-TO"
_HEADER_SPEC = f"BODY.PEEK[HEADER.FIELDS ({_HEADER_FIELDS})]"

_UID_RE = re.compile(rb"UID (\d+)")
_THRID_RE = re.compile(rb"X-GM-THRID (\d+)")


class ImapAdapter(MailAdapter):
    """Concrete IMAP adapter with a lazily-created, reused connection."""

    def __init__(self, account, source_tag, host, user, password, port=993,
                 conn_factory=None):
        self.account = account
        self.source_tag = source_tag
        self.host = host
        self.user = user
        self.password = password
        self.port = port
        self._factory = conn_factory or (lambda h, p: imaplib.IMAP4_SSL(h, p))
        self._connection = None

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

    def capabilities(self) -> set:
        return {"raw_query", "server_side_categories", "server_threads"}

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
        return set()

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
        name = line.rsplit(b'"', 2)
        if len(name) >= 2:
            folders.append(name[-2].decode())
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
