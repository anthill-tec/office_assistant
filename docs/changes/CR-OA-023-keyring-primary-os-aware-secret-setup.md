# CR-OA-023 — Keyring-primary secret store + OS-aware `setup` (drop the vault backends)

**Status:** PENDING
**Type:** feature
**Priority:** High
**Depends on:** 020
**Labels:** mail, secrets, keyring, setup, packaging, axi
**Phase:** Wave 10 (embedded mail send)
**Design reference:** [DN-mail-access.md](../research/DN-mail-access.md) §Decision 8 (supersedes §Decision 4) · §Decision 6 (mail-auth/doctor)
**Author:** Antony John · **Co-author:** Vidushi (orchestrator — office-assistant)

## Context

CR-OA-020 §S4 shipped a **vault-first** secret resolver (`1Password op` / `Bitwarden bw` PRIMARY →
keyring → 0600 file), provisioned nowhere by `voa setup` and falling through **silently** to the least-secure
file store. First real usage exposed the cost/benefit inversion (DN §Decision 8): **Bitwarden is structurally
unusable** — `bw` needs an ephemeral `BW_SESSION` unlocked per process every session; an API token logs in
but cannot unlock — and **1Password** service-account + dedicated-vault setup is heavy ceremony for a
single-user tool. Meanwhile `keyring` is an **opt-in `[mail]` extra**, so a bare `uv tool install vidushi-oa`
lands with **no secret store at all** (the same optional-dependency gap CR-OA-018 fixed for `pymongo`), and
nothing detects the host's actual Secret Service or guides the user to it. This CR implements DN §Decision 8:
**OS keyring is primary (a base dependency), the 0600 file is an explicit confirmed choice, the vault backends
are removed, and `setup`/`mail-auth` become OS-aware with a pre-flight.**

## Scope

### §S1 Remove the vault backends — resolver reduces to keyring → file
Delete `OnePasswordBackend`, `BitwardenBackend`, the `op://` reference routing, and their
`VIDUSHI_SECRET_BACKEND` registry entries + auto-detect from `SecretResolver`. The precedence chain becomes
**keyring (primary) → 0600 file (last resort)**. Remove the two symbols from `vidushi_oa/mail/__init__.py`
exports; update the docstrings in `secrets.py`, `accounts.py`, `factory.py` that name vaults.
**Surfaces (verified 2026-07-27):** `vidushi_oa/mail/secrets.py` (classes + registry L162–163 + auto-detect
L176 + `op://` route L191–195), `vidushi_oa/mail/__init__.py` (exports L5/8/18/19), docstrings in
`accounts.py:5` / `factory.py:73`; tests `test_cr_oa_020_secrets.py`, `test_cr_oa_020_mail_auth_doctor.py`,
`test_cr_oa_020_factory.py` revise with the implementation.

### §S2 `keyring` becomes a base dependency
Move `keyring>=24` from the `[mail]` optional extra into the base `[project] dependencies`. The `[mail]`
extra is retired (or reduced to nothing). A bare `uv tool install vidushi-oa` now ships a working secret
store. (`pymongo` stays optional — a genuine alternative backend; keyring is not.)

### §S3 OS-aware provisioning in `voa setup` / `voa mail-auth` + pre-flight
`setup`/`mail-auth` **detect the host OS + Secret-Service provider** and present the matching keyring path
with concrete guidance, then **pre-flight** the chosen backend (module present + provider reachable + a
`set`→`get` round-trip) before it is used:
- **KDE** → enable KWallet's Secret Service (claim `org.freedesktop.secrets`).
- **GNOME / other freedesktop** → gnome-keyring / libsecret.
- **macOS** → the login Keychain (native, no action).
- **headless / no provider** → the **0600 file**, offered as a **stated, confirmed user choice** — never a
  silent downgrade (DN §Decision 8: reaching the file backend is an explicit outcome).

### §S4 `voa doctor` reflects the simplified chain + explicit file surfacing
`doctor`'s secret-backend line reports **keyring** vs **file (confirmed)**; when no provider is wired it names
the active OS's fix (e.g. "enable KWallet Secret Service"). No `1password`/`bitwarden` kinds remain. Never
prints a secret.

All verbs stay AXI-conformant (CR-OA-017): TOON output, `--json`, structured errors, exit codes; `mail-auth`
keeps its single documented interactive exception (DN §Decision 6).

## Acceptance criteria

Tests run against **fakes / temp keyrings** (the `keyrings.alt` file backend on a hermetic temp path,
per the existing `[test]` extra) — no real OS Secret Service, no live vault.

### §S1
- [ ] `grep -rE "OnePasswordBackend|BitwardenBackend|op://" vidushi_oa/` returns **zero** matches; importing either name from `vidushi_oa.mail` raises `ImportError`.
- [ ] With a keyring backend available, `SecretResolver()._primary_backend().name == "keyring"`; with none available it is `"file"`. There is no code path that selects a `1password`/`bitwarden` backend.
- [ ] A ref of the form `op://…` is **not** specially routed — it resolves through the normal keyring→file chain (and yields the not-found error when absent), asserting the vault route is gone.

### §S2
- [ ] Built wheel/sdist metadata lists `keyring` in `Requires-Dist` **without** an `extra ==` marker (a base dependency), verified by **building the artifact and inspecting its `METADATA`** (not by parsing `pyproject.toml`). No `Provides-Extra: mail` carrying keyring remains.
- [ ] `python -c "import keyring"` succeeds in an environment installed from the built wheel with **no extras** requested.

### §S3
- [ ] Against a faked **KDE** environment (Secret-Service provider absent), `voa setup` (mail path) emits guidance naming **KWallet / `org.freedesktop.secrets`** and exits without silently writing a file-backend secret.
- [ ] Against a faked **no-provider / headless** environment, the file backend is chosen only via an **explicit confirmed** step (a flag/prompt); a structured status records `secret_backend: file` as a stated choice — asserted, and the silent-fallback path is absent (grep: no unconditional keyring→file fall-through without the confirmed marker).
- [ ] The pre-flight performs a `set`→`get` round-trip on the selected backend and reports success/failure in its TOON status.
- [ ] **Caller-existence:** `voa --help` shows the setup/mail-auth path; the OS-detection + pre-flight is invoked from a non-test caller in `vidushi_oa/_cli.py` (grep ≥1).

### §S4
- [ ] `voa doctor` on a keyring-available env reports `secret_backend: keyring`; on a no-provider env reports `secret_backend: file` **with a `confirmed`/explicit marker** and an OS-specific fix hint; the output contains neither `1password` nor `bitwarden`.

## Estimated size
M — a deletion (two backends + routing) plus new OS/desktop detection, a pre-flight round-trip, an explicit
file-choice gate, the doctor revision, and one packaging move (keyring → base dep).

## Risk
**Behavioural removal** — any existing `op://`/vault config stops resolving; acceptable pre-1.1.0 and the
single user is on the file backend (no vault in use). **OS detection** is inherently host-specific — mitigated
by testing against faked environments and degrading to the confirmed file choice on any unknown host.
**Silent-to-explicit file choice** is the safety win, not a regression. **Packaging** — moving keyring to a
base dep is verified against the built artifact's metadata.

## Non-goals
Remote secret managers (1Password/Bitwarden/cloud KMS) — **removed here**; a future CR may add one behind an
optional extra if ever needed. Changing the **store** backend selection (SQLite/Mongo — CR-OA-018).
Auto-installing OS packages or toggling desktop settings on the user's behalf (setup **guides**; the user
enables KWallet/gnome-keyring). Encrypting the file backend beyond `0600` fs perms.
