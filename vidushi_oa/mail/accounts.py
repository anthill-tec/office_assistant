"""Reference-only mail-account registry (CR-OA-020 §S5).

Persists the *configured mail accounts* as a small JSON list. Each entry carries
EXACTLY `{name, provider, address, secret_ref}` — a pointer to where the secret
lives (keyring/1Password/Bitwarden/file ref), NEVER the secret material itself.

Path resolution: the `VIDUSHI_MAIL_CONFIG` env var wins; otherwise
`$XDG_CONFIG_HOME/vidushi-oa/accounts.json` (falling back to
`~/.config/vidushi-oa/accounts.json`). The file and its parent directory are
created with owner-only permissions (`0600` on the file).
"""
import json
import os

_ENTRY_KEYS = ("name", "provider", "address", "secret_ref")


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


def add_account(name, provider, address, secret_ref, path=None) -> dict:
    """Append a reference-only account entry and persist it (file mode `0600`).

    Returns the stored entry. Existing entries are preserved in order.
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
    }
    accounts = load_accounts(target)
    accounts.append(entry)

    # Create the file owner-only before writing any content.
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(accounts, f, ensure_ascii=False, indent=2)
    os.chmod(target, 0o600)
    return entry
