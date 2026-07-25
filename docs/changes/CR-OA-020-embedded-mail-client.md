# CR-OA-020 — Embedded mail client in `voa` (unified Gmail/Fastmail/Yahoo + vault-first credentials)

**Status:** PENDING
**Type:** feature
**Priority:** High
**Depends on:** 017
**Labels:** mail, imap, jmap, credentials, vault, axi
**Phase:** Wave 9 (embedded mail)
**Design reference:** [DN-mail-access.md](../research/DN-mail-access.md) · [DN-agent-interface-toon.md](../research/DN-agent-interface-toon.md) · [DN-persistence-mongodb.md](../research/DN-persistence-mongodb.md)
**Author:** Antony John · **Co-author:** Vidushi (orchestrator — office-assistant)

## Context

Mail reading is the last mechanical task still done by the LLM (via mail MCPs), spending tokens on raw
email JSON + de-dup + source-tagging in-context, and it is the least portable part (the claude.ai Gmail
connector is harness-specific). Per DN-mail-access, `voa` gains an **embedded mail client** — a unified
interface over Gmail/Fastmail/Yahoo with **`mail-*` verbs** that return pre-filtered, merged, TOON-shaped
results — so the agent spends tokens only on judgment. Credentials resolve **vault-first** (1Password /
Bitwarden) with the OS keyring as fallback; `voa` holds only references, never secrets.

## Scope

### §S1 Unified `MailClient` interface + capability flags
A provider-agnostic interface (search / fetch-message / list-folders / list-accounts) that the CLI verbs
call, with **capability flags** (e.g. `server_side_categories`, `raw_query`, `server_threads`) an adapter
declares so callers degrade gracefully. Adapters are selected per configured account.

### §S2 IMAP adapters — Gmail (`X-GM-RAW`) + Yahoo (RFC 3501)
Stdlib `imaplib` adapters: **Gmail** uses `UID SEARCH X-GM-RAW "<gmail query>"` for full server-side Gmail
syntax (`category:`, `newer_than:`, `has:attachment`) and `X-GM-THRID` threads; **Yahoo** uses plain RFC
3501 `SEARCH` with **client-side threading** (from `References`/`In-Reply-To`), a **single reused
connection** (≤5 cap), and skips `Bulk Mail`. Both do header-only `FETCH (BODY.PEEK[HEADER.FIELDS …])` and
UID-based incremental sync (`UIDVALIDITY`+highest-UID cache).

### §S3 Fastmail JMAP adapter (thin-HTTP) + IMAP fallback
A thin JMAP client (over `httpx`, or stdlib `urllib`): `GET /jmap/session` (cache `apiUrl`+`accountId`),
then one `POST` of `Email/query` + a `#`-referenced `Email/get` requesting only needed properties (+
`SearchSnippet`) — pre-filtered, projected, one round-trip. Exposes threads and the **delivered-to alias**
as a correlation key. Falls back to the IMAP adapter for Basic-plan accounts without an API token.

### §S4 Vault-first pluggable secret resolver
A secret resolver with the **precedence chain** `configured vault → OS keyring → 0600 file`
(`VIDUSHI_SECRET_BACKEND` names the primary). Backends: **`1password`** (`op read op://…`, service-account
token, zero Python deps), **`bitwarden`** (`bw get …`, Vaultwarden-capable, zero Python deps),
**`keyring`** (fallback), **`file`** (encrypted-at-rest last resort). `voa` stores only a **reference**;
the resolved secret lives in memory for the run and is **never** written to the store, snapshots, config,
or logs. An unavailable/unconfigured vault falls back with a **warning**, not a crash.

### §S5 `mail-*` verbs (AXI/TOON)
`mail-auth` (register a credential *reference* for an account: provider, address, secret-ref),
`mail-accounts` (list configured accounts + capabilities), `mail-search` (server-side search across
selected accounts → **merge + de-dup by `Message-ID` + `[FM]`/`[GM]`/`[YH]` source-tag + field-project +
TOON envelope**), and `mail-get` (one message by account+uid/id). All verbs are AXI-conformant
(CR-OA-017): TOON envelope (`results`/`tally`/`next`) on reads, `--json` bare array, structured errors to
stdout.

### §S6 Gmail Workspace XOAUTH2 fallback
For Workspace accounts with app passwords disabled, an IMAP **XOAUTH2** auth path (build the SASL
`XOAUTH2` string from an OAuth access token refreshed via a minimal `httpx` call — **no**
`google-api-python-client`). Selected per-account when configured.

### §S7 Interactive `voa mail-auth` + `voa doctor`
`voa mail-auth` (DN-mail-access Decision 6) registers a provider credential: it prompts for the secret via
**hidden input** and stores only a **reference** in the active backend (vault or keyring), detecting/
configuring the dedicated read-only vault (1Password service-account token / Bitwarden session) and
**defaulting to keyring** when none is provisioned. Its **interactive secret prompt is the single documented
exception to AXI #6** (secure entry — the secret never enters argv/history/env/agent context); it still
emits an **AXI TOON** status object, honours `--json`, and provides a **non-interactive escape** (secret via
stdin/env) for automation/CI. `voa doctor` is a gh-axi-style diagnostic — engine, active store + secret
backends + reachability, each configured provider (credential kind + whether its reference resolves) with a
fix hint — as a **full AXI/TOON** read (absorbs `setup --check`), never revealing secrets. Non-interactive
store provisioning stays in `voa setup` (§S3, CR-018). All other `mail-*` verbs are AXI-conformant per
CR-OA-017.

## Acceptance criteria

Adapter/verb tests run against **fakes** (an in-process fake IMAP server / a fake JMAP HTTP endpoint / a
fake `op`/`bw` on `PATH`) — no live credentials in the suite. Live-account verification is a documented
**manual** step, not a gate.

### §S1
- [ ] A `MailClient` interface type exists with the four operations and a `capabilities()` set; a registered fake adapter is dispatched by account; `mail-search` across two fake accounts returns a single merged result set.

### §S2
- [ ] Against a fake IMAP server, the **Gmail** adapter issues an `X-GM-RAW` search for a Gmail-syntax query and parses the matched UIDs' headers into the common message shape; the **Yahoo** adapter issues an RFC 3501 `SEARCH` (no `X-GM-*`), reconstructs a thread from `References`, and uses one reused connection (a test asserts no second connect).

### §S3
- [ ] Against a fake JMAP endpoint, the Fastmail adapter performs `session → Email/query → #-ref Email/get` in **one POST**, projects only requested properties, and surfaces the delivered-to alias; a Basic-plan (token-absent) config falls back to the IMAP adapter.

### §S4
- [ ] Precedence: with a fake `op` present the resolver returns the vault value; with the vault CLI absent/unset it **falls back to keyring** (fake) and emits a warning; with both absent it uses the file backend. A test asserts the resolved secret value is **never** written to the store, config, snapshot, or captured logs (grep the artifacts for the sentinel secret → 0).
- [ ] `mail-auth` persists only a **reference** (provider + address + `secret-ref`), never the secret; a test reads back the stored account and asserts no secret material is present.

### §S5
- [ ] `mail-search` across fake FM+GM+YH accounts returns a TOON envelope with `results[N]` + `tally:` + `next[N]`; each row is `[FM]`/`[GM]`/`[YH]` source-tagged; two accounts returning the same `Message-ID` de-dup to one row; `--json` yields a bare array with no `tally`.
- [ ] **Caller-existence:** `voa --help` lists `mail-search`/`mail-auth`/`mail-accounts`/`mail-get`/`doctor`, and each verb is wired via a non-test `set_defaults` caller (grep ≥1).

### §S6
- [ ] Given an access token, the XOAUTH2 adapter builds the correct base64 `user=…\x01auth=Bearer …\x01\x01` SASL string and authenticates against the fake IMAP server via `AUTHENTICATE XOAUTH2`; a unit test asserts the encoded string.

### §S7
- [ ] `voa mail-auth` registers a provider credential storing only a **reference** (never the secret in argv/store/config/logs), emits an AXI TOON status object, and honours `--json`; its **non-interactive** mode (secret via stdin) registers the same reference headlessly — a test drives the stdin path and greps every artifact for the sentinel secret → 0 matches.
- [ ] With no vault provisioned, `mail-auth` **falls back to keyring** with a warning (fake vault CLI absent) — not a crash.
- [ ] `voa doctor` emits a TOON envelope reporting engine + active store backend + active secret backend + each configured provider (credential kind + whether the reference resolves), flags a missing/broken config with a fix hint, exits non-zero when a checked item fails, `--json` yields clean JSON, and it **prints no secret value** (a test seeds a sentinel + greps `doctor` output → 0 matches).

## Estimated size
L–XL — a mail subsystem (unified interface + three provider adapters over two protocols), a four-backend
secret resolver, four AXI verbs, and the XOAUTH2 fallback. Cycle-plan: interface+IMAP (§S1–S2) → JMAP
(§S3) → secret resolver (§S4) → `mail-*` verbs (§S5) → XOAUTH2 (§S6).

## Risk
Testing without live accounts — mitigated by in-process fakes + a documented manual live-verify checklist.
Credential security — mitigated by vault-first + reference-not-persist + read-only scopes + the
never-in-artifacts AC. JMAP youth — mitigated by the thin-HTTP (no library pin) + IMAP fallback. Yahoo
weakness/limits — capability-flag degradation + single reused connection. Provider auth drift — IMAP is
stable; the XOAUTH2 path hedges Google's OAuth-only signalling.

## Non-goals
Sending mail (read-mostly; SMTP / JMAP `EmailSubmission` deferred); a live-sync/IDLE daemon (poll-on-demand
only); the skill revision that repoints "Mailboxes & search" to `voa mail-*` (CR-OA-021); calendar/contacts
over JMAP; a non-claude.ai Gmail *connector* (embedding replaces the connector entirely).
