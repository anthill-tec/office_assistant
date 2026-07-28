"""Draft -> store-row link persistence (CR-OA-022 §S5).

Bridges a draft id created by ``mail-draft``/``mail-reply`` to the store row it was
drafted for (``--case``/``--invoice``/…), so that when ``mail-send`` dispatches that
draft it can record the sent message as a ``document`` on the linked row and resolve
the row's correspondence action — the tracked correspondence trail.

Each entry is keyed by ``draft_id`` and stores ``{"fk_field": <store type, e.g.
"cases">, "fk_id": <row id, e.g. "case_x">}``. ``save_link`` persists one; ``pop_link``
returns and REMOVES it (a link is consumed exactly once, on the send that follows the
draft). The links live in a small JSON file next to the mail-account registry, so the
same ``VIDUSHI_MAIL_CONFIG``/``XDG_CONFIG_HOME`` isolation the accounts registry honours
covers the link store too (the test suite points both at throwaway tmp paths).
"""
import json
import os

_FILENAME = "draft_links.json"


def _links_path() -> str:
    """Resolve the link-store path, mirroring the accounts registry's isolation.

    Placed in the same directory the mail-account registry resolves to
    (``VIDUSHI_MAIL_CONFIG`` dir wins; otherwise ``$XDG_CONFIG_HOME/vidushi-oa``,
    falling back to ``~/.config/vidushi-oa``)."""
    env = os.environ.get("VIDUSHI_MAIL_CONFIG")
    if env:
        parent = os.path.dirname(env) or "."
        return os.path.join(parent, _FILENAME)
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config")
    return os.path.join(base, "vidushi-oa", _FILENAME)


def _load(target: str) -> dict:
    if not os.path.exists(target):
        return {}
    with open(target, encoding="utf-8") as f:
        return dict(json.load(f))


def _store(target: str, links: dict) -> None:
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(links, f, ensure_ascii=False, indent=2)
    os.chmod(target, 0o600)


def save_link(draft_id: str, fk_field: str, fk_id: str) -> None:
    """Persist a ``draft_id`` -> ``{fk_field, fk_id}`` link (owner-only file)."""
    target = _links_path()
    links = _load(target)
    links[draft_id] = {"fk_field": fk_field, "fk_id": fk_id}
    _store(target, links)


def pop_link(draft_id: str) -> dict | None:
    """Return and REMOVE the link for ``draft_id``, or ``None`` if none exists."""
    target = _links_path()
    links = _load(target)
    link = links.pop(draft_id, None)
    if link is None:
        return None
    _store(target, links)
    return {"fk_field": link["fk_field"], "fk_id": link["fk_id"]}
