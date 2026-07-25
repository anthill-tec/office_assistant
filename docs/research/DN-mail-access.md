# DN — Embedded mail access in the engine (unified client over Gmail / Fastmail / Yahoo)

> **Type:** DN (design note) · **Status:** ACCEPTED (2026-07-25)
> **Author:** Antony John · **Co-author:** Vidushi (orchestrator — office-assistant)
> **Informs:** the Wave 9 mail CRs · **Supersedes:** CR-OA-019 (declared mail-MCP prerequisites)
> **Related:** [DN-agent-interface-toon.md](DN-agent-interface-toon.md) · [DN-persistence-mongodb.md](DN-persistence-mongodb.md) · [DN-packaging-distribution.md](DN-packaging-distribution.md)

## Context

Reading mail is the **last mechanical task still done by the LLM**: the skill drives mail MCP services
(FastmailMCP + the claude.ai Gmail connector), so raw email JSON, de-dup, source-tagging, and
classification all happen **in-context**, spending tokens on work a tool could do deterministically. It
is also the least portable part — the Gmail claude.ai connector is harness-specific and not installable.
Moving mail into the engine (`voa`) turns "read my mail" into a pre-computed, TOON-shaped tool call,
consistent with the framework principle (DN-persistence-mongodb): *the deterministic work lives in the
backend; the agent spends tokens only on judgment.* This DN supersedes the declared-MCP-prerequisites
approach (CR-OA-019) with embedded clients.

Findings below are from a primary-source research pass (2026-07-25) across the three providers and the
two vaults; the cited URLs are recorded per decision.

## Decision 1 — embed mail access in `voa` (supersede the MCP-prerequisite approach)

`voa` gains a mail subsystem and `mail-*` verbs; the skill calls those instead of mail MCPs. The engine
becomes mail-aware (it was mail-agnostic before). CR-OA-019's "declare FastmailMCP + Gmail connector as
prerequisites" is **superseded** — there is no MCP prerequisite once the client is embedded.

## Decision 2 — hybrid protocol: IMAP common denominator + JMAP for Fastmail

A unified `MailClient` interface modelled on the **IMAP lowest-common-denominator** (folders + RFC 3501
`SEARCH` + UID sync + client-side threading), with per-provider adapters and **capability flags** for
extras a provider supports:

- **Gmail** — stdlib `imaplib` + the **`X-GM-RAW`** extension, which runs the *full Gmail search syntax*
  server-side (`category:`, `newer_than:`, `has:attachment`, `label:`) — as capable as the Gmail API for
  our needs, at **zero dependencies**. (The official Gmail API path pulls ~20+ transitive packages incl.
  `protobuf` — the same weight as the MCP SDK we already rejected; declined.)
- **Fastmail** — **thin-HTTP JMAP** (over `httpx`, or stdlib `urllib` for zero deps) with a **read-only
  JMAP API token**: `Email/query` + a `#`-referenced `Email/get` returns pre-filtered, field-projected
  results in **one round-trip**, with first-class threads and the **delivered-to alias as a correlation
  key** (the masked-alias trick). Falls back to IMAP for Basic-plan accounts that can't mint an API token.
- **Yahoo** — stdlib `imaplib` (no Yahoo Mail API exists in 2026). Plain RFC 3501 only (no categories,
  client-side threading), a ~3–5 concurrent-connection cap (use one reused connection), skip `Bulk Mail`,
  poll rather than IDLE. It reports the missing capabilities via the capability flags so the interface
  degrades gracefully.

Rationale: IMAP unifies all three (and the auth), keeps deps near zero, and — via `X-GM-RAW` — keeps
Gmail's server-side power. JMAP is the one place a richer protocol earns its single small dependency.

## Decision 3 — auth: app passwords + a Fastmail read-only token

- **Gmail** (consumer `@gmail.com`): IMAP **app password** (requires 2FA; the legacy "less secure apps"
  path was removed in 2025, app passwords survived). **Workspace** accounts whose admin disables app
  passwords use **IMAP + XOAUTH2** (still `imaplib` + a minimal token refresh over `httpx` — no
  `google-api-python-client`).
- **Fastmail**: a **read-only JMAP API token** (scoped, cannot mutate/send); IMAP app password as fallback.
- **Yahoo**: IMAP **app password** (2FA required; no usable OAuth).

The **user generates** every app-password/token in the provider's own settings and hands it to `voa`;
`voa` never types a credential into any provider site (that stays prohibited). Prefer read-only scopes.

## Decision 4 — vault-first pluggable secret resolver (keyring is the fallback)

Credentials are resolved through a **precedence chain**, not stored by `voa` itself — `voa` holds only a
**reference**, the vault holds the secret, resolved at runtime and never persisted:

```
configured vault  (1password `op`  │  bitwarden `bw`)   ← PRIMARY store
   └─ if the CLI is missing / token unset / unreachable ↓
OS keyring                                              ← fallback (graceful, with a warning)
   └─ if unavailable ↓
0600 encrypted-at-rest file                             ← last resort
```

- **`VIDUSHI_SECRET_BACKEND`** names the primary vault; keyring catches the not-set-up case instead of
  hard-failing. The vault is a **dedicated, read-only** store the user hosts on 1Password/Bitwarden
  (provisioned during `voa setup` — Decision 6); `voa` connects to it, it does not create it. Keyring
  exists so `voa` still works before a vault is configured.
- **1Password** — `op read "op://voa-secrets/<item>/<field>"` via the `op` CLI (**zero Python deps**),
  authenticated by a **service-account token** (`OP_SERVICE_ACCOUNT_TOKEN`), read-only, scoped to a
  **dedicated vault** (service accounts *cannot* read the built-in Private vault — the user puts `voa`'s
  secrets in a purpose-made vault). The Rust-native Python SDK is declined (glibc/libssl floors, async).
- **Bitwarden** — `bw get` via the `bw` CLI (**zero Python deps**), **Vaultwarden / self-hosted capable**.
  Honest caveat: `bw` needs the master password at unlock and its `BW_SESSION` doesn't persist across
  shells, so a bootstrap secret (master password or cached session) lives locally; its real value is
  **centralization + self-hosting**, not zero-local-secret. (Bitwarden Secrets Manager + its native SDK is
  a separate paid product Vaultwarden doesn't implement — declined.)
- **keyring** — the OS secret service via the `keyring` library (~1 small dep); the fallback.
- **file** — a `0600`, encrypted-at-rest local file (stdlib); last resort / CI.

Inherent truth to state plainly: **some bootstrap secret always lives on the machine** (a keyring entry,
a 1Password service-account token, or a Bitwarden master-password/session). The vault backends centralize
and scope it; they do not eliminate it.

## Decision 5 — token-saving unified interface (`mail-*` verbs)

The engine exposes `mail-*` verbs that run **server-side search per provider**, then **merge + de-dup +
`[FM]`/`[GM]`/`[YH]` source-tag + project only needed fields + emit TOON**. The agent receives
pre-filtered, pre-merged, token-frugal rows — never raw email JSON. Verbs are AXI-conformant (CR-OA-017)
like every other `voa` verb. The `mail-*` surface feeds the existing domain flow (findings persisted via
the store), so mail becomes another deterministic tool, not an in-context chore.

## Decision 6 — interactive `voa mail-auth` + a `voa doctor` diagnostic

Credential provisioning is an **interactive `voa mail-auth`** command plus a `voa doctor` health check — a
post-install, agent-*guided* flow, **not** part of the package install (the store's own provisioning stays
the non-interactive `voa setup`, DN-packaging-distribution Decision 5):

- **`voa mail-auth` is interactive — a deliberate, scoped exception to AXI #6.** The user runs it per
  provider; it prompts for the secret via **hidden input** and stores only the **reference**. Making *this
  one* command interactive is a **security win**: the secret passes **directly from the user into `voa`**,
  never through argv, shell history, env, or the agent's context (the agent guides *which* provider and
  *how to generate* the token, but never handles the secret). Precedented by `gh auth login` (interactive)
  in the same AXI ecosystem. A **non-interactive escape** (secret via stdin/env) remains for automation/CI.
  Every data/query verb stays strictly AXI-non-interactive; `mail-auth` is the single documented exception.
- **Vault backend + fallback:** `mail-auth` detects/configures the **dedicated, read-only** vault the user
  hosts on **1Password** (a service-account token scoped to that vault) or **Bitwarden** (session /
  self-hosted); `voa` connects to it, never creates it. **If no vault is provisioned, it defaults to the
  local OS keyring** (Decision 4's fallback).
- **Per-provider credential kind (the user generates it on the provider's site):**
  - **Fastmail** → **token-based API access** (a read-only JMAP token).
  - **Gmail** → **login access** (an IMAP app password; XOAUTH2 for a Workspace account with app passwords
    disabled).
  - **Yahoo** → **login access** (an IMAP app password).
  The agent presents the concrete generation steps; the user generates each and enters it via the
  interactive `mail-auth`.
- **`voa doctor` — install/config health (gh-axi style).** A diagnostic verb reporting: engine version, the
  active store backend + reachability, the active secret backend (vault reachable? else keyring), and each
  configured mail provider + credential kind + whether its reference **resolves** — flagging missing/broken
  config with a fix hint. TOON output; **never reveals a secret**. (Absorbs the earlier `setup --check`.)

## Consequences

- **Dependency footprint: ~0–2 small deps** — `httpx` for Fastmail JMAP (or `urllib`, 0), optional
  `keyring` for the fallback; the vault backends and both IMAP adapters add **zero** (stdlib + external
  CLIs the user already has). Consistent with the lean ethos that dropped MCP and the Gmail API.
- **Supersedes CR-OA-019** (MCP prerequisites) — mark it SUPERSEDED.
- **The skill changes** — `SKILL.md`'s "Mailboxes & search" switches from MCP calls to `voa mail-*`
  verbs (a later skill-revision CR); the safety contract (phishing/customs) is unchanged.
- **Independent of Wave 8** — CR-OA-017/018 (AXI + backend/packaging) are unaffected; mail is Wave 9.
- **No credentials in any artifact** — never in the store, snapshots, packages, or git; only references.

## Risks

- **Credential security** is the real cost (`voa` can reach mailboxes). Mitigated by vault-first resolution
  + read-only scopes + never-persist + the user generating each secret. The bootstrap-secret-on-machine
  truth is documented, not hidden.
- **Gmail Workspace** app-password disablement → the XOAUTH2 fallback path must exist.
- **Yahoo** is the weak sibling (no categories/threads, connection cap) — capability-flag degradation.
- **JMAP** libraries are young; mitigated by the thin-HTTP approach (we own ~200 LOC, no library pin) +
  the IMAP fallback.
- **Provider auth drift** (app-password / token policy changes) — IMAP is very stable; watch Google's
  periodic OAuth-only signalling.

## Research provenance (primary sources, 2026-07-25)

- Gmail IMAP extensions (`X-GM-RAW`/labels): developers.google.com/workspace/gmail/imap/imap-extensions ·
  app-passwords/basic-auth wind-down: support.google.com/a/answer/14114704
- Fastmail JMAP (session/bearer, thin-HTTP): fastmail.com/for-developers/integrating-with-fastmail ·
  API tokens + read-only scope + plan gate: fastmail.help API-tokens · JMAP mail: RFC 8621
- Yahoo IMAP-only + app password: help.yahoo.com (sln28681, SLN15241) · no proprietary Mail API:
  senders.yahooinc.com/developer/developer-access
- 1Password `op` secret references + service accounts: developer.1password.com (secret-reference-syntax,
  service-accounts) — service accounts can't read the Private vault
- Bitwarden `bw` CLI + self-hosted/Vaultwarden: bitwarden.com/help/cli · Secrets Manager/SDK is a separate
  product: bitwarden.com/help/secrets-manager-cli
