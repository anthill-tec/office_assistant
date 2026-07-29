"""Reference-only mail-account registry (CR-OA-020 §S5).

Persists the *configured mail accounts* as a small JSON list. Each entry carries
EXACTLY `{name, provider, address, secret_ref, auth_mode}` — a pointer to where the
secret lives (keyring/file ref), NEVER the secret material
itself. `auth_mode` selects how the resolved secret authenticates (`password` —
the default — or `xoauth2`, Gmail Workspace's refresh-token flow); entries written
before `auth_mode` existed load as `password`.

Path resolution: the `VIDUSHI_MAIL_CONFIG` env var wins; otherwise
`$XDG_CONFIG_HOME/vidushi-oa/accounts.json` (falling back to
`~/.config/vidushi-oa/accounts.json`). The file and its parent directory are
created with owner-only permissions (`0600` on the file).
"""
import json
import os

_ENTRY_KEYS = ("name", "provider", "address", "secret_ref", "auth_mode", "send",
               "aliases", "endpoint")


def _config_path(path=None) -> str:
    """Resolve the accounts-registry path (explicit arg > env > XDG default)."""
    if path is not None:
        return str(path)
    env = os.environ.get("VIDUSHI_MAIL_CONFIG")
    if env:
        return env
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config")
    return os.path.join(base, "vidushi-oa", "accounts.json")


def load_accounts(path=None) -> list[dict]:
    """Return the registered accounts in append order, or `[]` if none/absent."""
    target = _config_path(path)
    if not os.path.exists(target):
        return []
    with open(target, encoding="utf-8") as f:
        data = json.load(f)
    return list(data)


def add_account(name, provider, address, secret_ref, auth_mode="password",
                send=False, aliases=None, endpoint=None, path=None) -> dict:
    """Upsert a reference-only account entry by name and persist it (mode `0600`).

    Returns the stored entry. An existing entry with the same `name` is replaced
    in place (order preserved) so re-running `voa mail-auth` to rotate a secret
    stays idempotent; otherwise the entry is appended. `auth_mode` records how the
    resolved secret authenticates (`password` default, or `xoauth2`). `send` is the
    opt-in per-account send-capability flag (default `False` — read-only accounts
    stay read-only; entries written before it existed are treated as `False`).
    `aliases` is the configured list of additional From identities for this account
    (Fastmail masked aliases, etc.); it defaults to an empty list.

    `endpoint` is an OPTIONAL per-account provider-endpoint override mapping (any of
    `jmap_url` / `imap_host` / `imap_port` / `smtp_host` / `smtp_port` / `tls_verify`)
    pointing the adapters at a local emulator instead of the hardcoded real providers.
    An absent / `None` override is OMITTED from the entry entirely — never stored as an
    empty mapping — so a bare install's persisted schema is byte-for-byte as before. On
    a re-registration (a secret rotation via `mail-auth`, or `doctor --fix`) an absent
    override does not clear a previously configured one: since the matched entry is
    replaced wholesale, the prior `endpoint` is carried forward.

    `None` (not supplied) and `{}` (supplied empty) are therefore DISTINCT: an empty
    mapping is an explicit CLEAR, dropping any previously configured override. Without
    that distinction `tls_verify: false` — a persisted, per-account disabling of
    certificate/hostname verification — would be unremovable except by hand-editing the
    accounts file, which the project's cardinal rules forbid.
    """
    target = _config_path(path)
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)

    accounts = load_accounts(target)
    index = next((i for i, e in enumerate(accounts) if e.get("name") == name), None)
    # The matched entry is replaced WHOLESALE, so an absent override would silently
    # drop a configured one on every re-registration (a secret rotation, `doctor
    # --fix`) — carry the prior one forward instead. An explicitly EMPTY mapping is
    # the deliberate clear, so only `None` carries forward.
    if endpoint is None and index is not None:
        endpoint = accounts[index].get("endpoint")

    entry = {
        "name": name,
        "provider": provider,
        "address": address,
        "secret_ref": secret_ref,
        "auth_mode": auth_mode,
        "send": bool(send),
        "aliases": list(aliases or []),
    }
    # An endpoint override is stored ONLY when configured — an absent one is OMITTED,
    # never fabricated, so a real account's schema is byte-for-byte as before.
    if endpoint:
        entry["endpoint"] = dict(endpoint)
    if index is None:
        accounts.append(entry)
    else:
        accounts[index] = entry

    # Create the file owner-only before writing any content.
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(accounts, f, ensure_ascii=False, indent=2)
    os.chmod(target, 0o600)
    return entry
