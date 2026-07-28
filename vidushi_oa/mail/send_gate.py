"""Send-capability gate (CR-OA-022 §S1).

Per DN-mail-access.md §Decision 7, sending is opt-in per account: a read-only
account stays read-only, and the send verbs must refuse an account that was not
registered with the `send` capability flag. `ensure_send_capable` is that guard —
the §S3 send verbs call it before dispatching any message.
"""


def ensure_send_capable(entry: dict) -> None:
    """Raise `PermissionError` unless `entry` is a send-capable account entry.

    An account is send-capable only when its registry entry carries
    ``send is True``; a missing or falsey `send` key (e.g. a legacy read-only
    account) is refused with a message that names "send".
    """
    if entry.get("send") is not True:
        name = entry.get("name") or entry.get("address") or "<unknown>"
        raise PermissionError(
            f"account {name!r} is not send-capable: re-register it with "
            f"`voa mail-auth --send` to grant send permission"
        )
