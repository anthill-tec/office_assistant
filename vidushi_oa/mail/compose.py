"""CR-OA-022 §S2 — RFC 5322 composition + reply threading + From-identity.

Pure, provider-agnostic message construction. `compose()` builds a valid
RFC 5322 message (via :class:`email.message.EmailMessage`) and returns its
serialized bytes; `validate_from()` guards that an outbound `From` is one of
the account's own identities (its address + configured aliases) before a
transport ever sends it.

No personal data is baked in here: every From/To/Cc/alias value is a caller
argument. The only address literals in this module are placeholder examples
using an ``example.com`` host, e.g.::

    compose(from_addr="you@example.com", to="vendor@example.com",
            subject="Re: order", body="Thanks.")
"""
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr


def _message_id_for(from_addr):
    """Mint a Message-ID scoped to *from_addr*'s domain.

    RFC 5322 §3.6.4 wants the right-hand side to be a domain the sender owns, and
    the stdlib default (`socket.getfqdn()`) would additionally bake the user's
    machine name into every outbound message. Falls back to the stdlib default
    only when *from_addr* carries no parsable domain.
    """
    domain = parseaddr(from_addr)[1].rpartition("@")[2].strip()
    return make_msgid(domain=domain) if domain else make_msgid()


def compose(
    from_addr,
    to,
    subject,
    body,
    cc=None,
    in_reply_to=None,
    references=None,
    attachments=None,
):
    """Build an RFC 5322 message and return its serialized bytes.

    Sets ``From``/``To``/``Subject`` and a plain-text body. ``Cc`` is added only
    when *cc* is given. Reply threading headers are added only when given:
    ``In-Reply-To`` from *in_reply_to*, and ``References`` from *references*
    (a list of Message-IDs is space-joined; a string is used as-is).

    The RFC 5322 §3.6.4 originator headers ``Date`` and ``Message-ID`` are always
    set — `EmailMessage` mints neither. Besides being mandatory, they keep two
    identical calls from serializing to identical bytes, which a content-addressed
    blob store (Fastmail/Cyrus JMAP) would otherwise collapse onto one message.

    *attachments* is an optional list of ``(filename, bytes)`` pairs; each is
    added as an ``application/octet-stream`` part.
    """
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = _message_id_for(from_addr)
    msg.set_content(body)

    if cc:
        msg["Cc"] = cc
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        if isinstance(references, str):
            msg["References"] = references
        else:
            msg["References"] = " ".join(references)

    for filename, data in attachments or []:
        msg.add_attachment(
            data,
            maintype="application",
            subtype="octet-stream",
            filename=filename,
        )

    return msg.as_bytes()


def validate_from(from_addr, identities):
    """Ensure *from_addr* is one of the account's own *identities*.

    Raises :class:`ValueError` naming *from_addr* when it is not a member of the
    *identities* collection (the account address + configured aliases); returns
    ``None`` quietly when it is a member.
    """
    if from_addr not in identities:
        raise ValueError(
            f"From address {from_addr!r} is not one of the account's identities"
        )
    return None
