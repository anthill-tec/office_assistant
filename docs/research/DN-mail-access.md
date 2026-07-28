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

**Harness-provided mail is a documented *alternative*, not the default (decided 2026-07-26).** The primary
target harnesses are **Hermes, Pi, Claude Code, and OpenCode** — skill-consuming, CLI-capable agent
runtimes that do **not** uniformly ship a mail capability. The embedded `voa` client is therefore the
**primary** path: it is the one **uniform, portable** way to read mail across all of them (each simply runs
the `voa` CLI + the skill), and the only path that delivers the **token-saving pre-processing** (server-
side search + merge + de-dup + TOON, so the agent never handles raw email) and **credential ownership** (no
dependency on a third-party mail MCP). Some harnesses *do* provide mail via **MCP** (e.g. OpenClaw's
`agent_mail`, Claude Code's FastmailMCP) — there is no magic built-in connector, so "the harness handles
connectivity" means "a mail MCP is configured for it." Where that's the case, the skill MAY delegate to
that MCP as a documented alternative (CR-OA-021); the embedded client stays the default and the only path
that yields the token win.

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

> **Superseded by [Decision 8](#decision-8--supersede-decision-4-keyring-primary-os-aware-setup-drop-the-vault-backends) (2026-07-27).** The vault backends (1Password/Bitwarden) are dropped; keyring becomes the primary (a base dep) and the 0600 file an explicit last resort. The rationale below is retained for lineage.

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
- **Secret backend (revised by [Decision 8](#decision-8--supersede-decision-4-keyring-primary-os-aware-setup-drop-the-vault-backends)):** ~~`mail-auth` detects/configures the dedicated, read-only vault on 1Password/Bitwarden~~ — the vault backends are **dropped**. `mail-auth`/`setup` now do an **OS-aware keyring** provision (primary), falling to the 0600 file only as an **explicit, confirmed** user choice. See Decision 8.
- **Per-provider credential kind (the user generates it on the provider's site):**
  - **Fastmail** → **token-based API access** (a read-only JMAP token).
  - **Gmail** → **login access** (an IMAP app password; XOAUTH2 for a Workspace account with app passwords
    disabled).
  - **Yahoo** → **login access** (an IMAP app password).
  The agent presents the concrete generation steps; the user generates each and enters it via the
  interactive `mail-auth`.
- **`voa doctor` — install/config health (gh-axi style) + remediation entry point.** A diagnostic verb
  reporting: engine version, the active store backend + reachability, the active **secret backend**
  (keyring wired? else the confirmed file store — per [Decision 8](#decision-8--supersede-decision-4-keyring-primary-os-aware-setup-drop-the-vault-backends)),
  and each configured mail provider + credential kind + whether its reference **resolves**. Beyond flagging,
  it emits the **ordered remediation plan / wizard** of Decision 8 (agent-runnable vs human-input-required
  steps). TOON output; **never reveals a secret**. (Absorbs the earlier `setup --check`.)

## Decision 7 — sending: draft-then-confirm, embedded (JMAP `EmailSubmission` + SMTP), send-capable creds

**Why (gap):** Decisions 1–6 embedded the **read** side only; sending was deferred (a CR-OA-020 non-goal).
But the skill's **Support domain requires draft-then-confirm outbound mail** (RMA / claims / service — "reply
from the buying alias the vendor knows, **never auto-send**"), so sending is a **required system capability**
that today still routes through the **harness mail MCP** — leaving Decision 1's "embed mail to supersede the
MCP" **half-done and non-portable**. This decision closes it: embed sending in `voa`, with the safety spine
enforced at the engine level. (Approved 2026-07-27.)

- **Transport (parallels Decision 2's read hybrid).** Fastmail → **JMAP `EmailSubmission`** (native
  identities/aliases); Gmail / Yahoo → **SMTP submission** (STARTTLS :587 / SSL :465). Both reuse the
  Decision 4 secret resolver + the XOAUTH2 path; a Fastmail app-password can SMTP-send as the fallback.
- **Credential scope — revisits Decision 3.** Decision 3 preferred a **read-only** Fastmail token ("cannot
  mutate/send"); sending needs a **send-capable** credential — a Fastmail JMAP token scoped
  `mail`+`submission`, or the IMAP **app-password** (which already authorizes SMTP) for Gmail/Yahoo, or
  XOAUTH2 with the send scope. **Send is opt-in per account** (read-only accounts stay read-only); `mail-auth`
  records a **`send`-capability flag**, and the send verbs refuse a non-send-capable account. The user still
  generates every credential.
- **Draft-then-confirm is the safety spine (engine-enforced) — `voa` has NO path that sends without an
  explicit, identified `mail-send`:**
  - `mail-draft` composes an RFC 5322 message and **saves a real draft** into the account's Drafts (JMAP
    blob-upload of the raw RFC822 bytes + `Email/import` into the `drafts`-role mailbox with `$draft` /
    IMAP `APPEND` to Drafts) — reviewable in the user's own mail client; returns
    the draft id; **no network send**.
  - `mail-send <draft-id>` dispatches **only that identified draft** (JMAP `EmailSubmission/set` / SMTP).
  - `mail-reply` composes a **threaded** reply (`In-Reply-To`/`References` from a fetched message) as a draft.
  This two-step is the engine-level enforcement of the skill's "draft-then-confirm, never auto-send" rule.
  (Options weighed: a **real draft in the mailbox** vs a locally-staged send-queue — chose the real draft:
  the user reviews it where they already read mail; no local queue to trust.)
- **Identity / From — masked aliases.** The From is a **chosen identity** — the account address or a
  **Fastmail masked alias** ("the buying alias the vendor knows") — validated against the account's identities
  (JMAP `Identity/get` / a configured alias list); an unknown From is refused.
- **Guards (mirror the skill).** Draft/send only to a **verified `contact`** (else a structured error unless
  explicitly overridden); a message can be **linked to a store row** (`--case`/`--invoice`/…) and, on send,
  recorded as a `document` + the relevant action resolved on that row — the tracked correspondence trail.

## Decision 8 — supersede Decision 4: keyring-primary, OS-aware setup, drop the vault backends

**Why (revision).** Decision 4 made a hosted **vault** (1Password `op` / Bitwarden `bw`) the PRIMARY
secret store with keyring as a mere fallback. First real usage showed that inverted the cost/benefit:
**Bitwarden is structurally unusable for `voa`** — `bw` needs an ephemeral `BW_SESSION` unlocked *per
process every session*; an API token logs in but cannot unlock — and **1Password's** service-account +
dedicated-vault provisioning is heavy ceremony for a single-user personal tool. Both are **tedious to set
up and drive** for the value returned. This decision **drops both vault backends** and makes the **OS
keyring the primary** store, with an OS-aware setup that offers what the host actually provides.
(Approved 2026-07-27.)

- **Revised precedence chain — two backends, no vault:**
  ```
  OS keyring (via the `keyring` library)          ← PRIMARY (base dependency, not an extra)
     └─ if no Secret Service provider is wired ↓
  0600 file (encrypted-at-rest, fs-perms)          ← explicit, CONFIRMED last resort (never a silent downgrade)
  ```
- **`keyring` becomes a BASE dependency** (out of the `[mail]` extra). The product is mail-driven; a
  missing secret store is not an optional condition. (`pymongo` stays optional — a genuine alternative
  backend; keyring is not.) This also closes the **installer gap**: a bare `uv tool install vidushi-oa`
  no longer lands without a secret store.
- **OS-aware `voa setup` / `voa mail-auth` — offer what the host provides.** Setup **detects the host OS +
  desktop/Secret-Service provider** and presents the appropriate keyring path with concrete, actionable
  guidance: **KDE** → enable KWallet's Secret Service (claim `org.freedesktop.secrets`); **GNOME/other
  freedesktop** → gnome-keyring / libsecret; **macOS** → the login Keychain (native); **headless / no
  provider** → the 0600 file **as a stated, confirmed choice**. It **pre-flights** the chosen backend (module
  present + provider reachable + a set/get round-trip) rather than discovering failure at first mail call.
- **No silent fallback.** The Decision 4 chain fell keyring→file with only a stderr warning, so a user who
  wanted the keyring silently landed on the least-secure file store. Post-revision, reaching the file backend
  is an **explicit, confirmed** outcome surfaced by setup and reported by `voa doctor`.
- **Guided remediation is a wizard, not a wall of hints — and `voa doctor` is the detector + entry point.**
  The interactive steps here need **human input** (typing the secret into `mail-auth`; flipping a desktop
  KWallet toggle) and therefore **must not be run silently by the agent** (DN §Decision 6: the agent guides
  *which* and *how*, the human enters the secret). So `voa doctor` doesn't just print fix hints — it emits an
  **ordered, machine-readable remediation plan** where each step is classified **agent-runnable** (a
  non-interactive command the agent may run) vs **human-input-required** (enable the OS Secret Service; run
  the interactive `mail-auth` for account X — a *recommendation the agent walks the user through*, one step at
  a time via the AXI `next[]` chain). A `voa doctor --fix` / `voa setup` **wizard mode** *instantiates* that
  sequence — chaining into interactive `mail-auth`/provisioning for the human — rather than requiring the user
  to assemble the raw `env VIDUSHI_SECRET_BACKEND=keyring voa mail-auth --provider … --address …` invocation
  by hand. The agent's role is to **recommend and guide locally**; the human performs each input step.
- **Removed surface (revises Decisions 4 + 6):** `OnePasswordBackend`, `BitwardenBackend`, the `op://`
  reference routing, and their `VIDUSHI_SECRET_BACKEND` registry entries + auto-detect are **deleted**;
  Decision 6's "detects/configures the dedicated vault … defaults to keyring" reduces to
  keyring-primary/file-fallback. `op://`-style external references are a **non-goal** (a later CR can add a
  remote-secret-manager backend behind an extra if ever needed).

## Consequences

- **Dependency footprint: ~1–2 small deps** — `httpx` for Fastmail JMAP (or `urllib`, 0) + **`keyring`
  now a base dep** (Decision 8); both IMAP adapters add **zero** (stdlib). The dropped vault backends
  remove the external-CLI assumption entirely. Consistent with the lean ethos that dropped MCP and the
  Gmail API.
- **Supersedes CR-OA-019** (MCP prerequisites) — mark it SUPERSEDED.
- **The skill changes** — `SKILL.md`'s "Mailboxes & search" switches from MCP calls to `voa mail-*`
  verbs (a later skill-revision CR); the safety contract (phishing/customs) is unchanged.
- **Independent of Wave 8** — CR-OA-017/018 (AXI + backend/packaging) are unaffected; mail is Wave 9.
- **No credentials in any artifact** — never in the store, snapshots, packages, or git; only references.
- **The client carries no personal data — only field descriptions (portability + privacy invariant).** The
  `voa` client and everything it emits — prompts, `next[]` hints, help text, error messages, and the
  setup/mail-auth/doctor **wizard** — hardcode **no** mail addresses, masked aliases, domains, account names,
  or any personal identifier. Every such value is supplied by the user at runtime (interactive paste / their
  own config store) and lives **only** in the user's config + keyring, never in `vidushi_oa/` source. Prompts
  and hints tell the user **what to paste into each field, illustrated with an artificial example for format**
  ("your Fastmail address, e.g. `you@fastmail.com`"; "the app password you generated, e.g.
  `abcd-efgh-ijkl-mnop`") — the sample is **fictitious** (`example`-style placeholders), shown only to convey
  shape; it is never a pre-filled real value and is never persisted. Generic, reusable, **portable**. Provider
  *infrastructure* endpoints (e.g. `imap.gmail.com`) are not personal data and may be coded. This is what lets
  the public PyPI/GitHub artifact ship with zero of the user's information in it — the agent guides *which*
  field (with an example shape), the human supplies the real value.

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
- OpenClaw is an MCP-based agent harness — mail via MCP servers (Composio `agent_mail` / AgentMail /
  FastmailMCP) + a skill, not a built-in connector: composio.dev/toolkits/agent_mail/framework/openclaw ·
  agentmail.to/docs/integrations/openclaw · openclawlaunch.com/guides/openclaw-agent-harness
