"""Reference-only mail-account registry (CR-OA-020 §S5).

Persists the *configured mail accounts* as a small JSON list. Each entry carries
EXACTLY `{name, provider, address, secret_ref, auth_mode}` — a pointer to where the
secret lives (keyring/1Password/Bitwarden/file ref), NEVER the secret material
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

_ENTRY_KEYS = ("name", "provider", "address", "secret_ref", "auth_mode")


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
                path=None) -> dict:
    """Upsert a reference-only account entry by name and persist it (mode `0600`).

    Returns the stored entry. An existing entry with the same `name` is replaced
    in place (order preserved) so re-running `voa mail-auth` to rotate a secret
    stays idempotent; otherwise the entry is appended. `auth_mode` records how the
    resolved secret authenticates (`password` default, or `xoauth2`).
    """
    target = _config_path(path)
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)

    entry = {
        "name": name,
        "provider": provider,
        "address": address,
        "secret_ref": secret_ref,
        "auth_mode": auth_mode,
    }
    accounts = load_accounts(target)
    for i, existing in enumerate(accounts):
        if existing.get("name") == name:
            accounts[i] = entry
            break
    else:
        accounts.append(entry)

    # Create the file owner-only before writing any content.
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(accounts, f, ensure_ascii=False, indent=2)
    os.chmod(target, 0o600)
    return entry
