# CR-OA-022 — Embedded mail *sending* in `voa` (draft-then-confirm, per-mailbox identity, store-linked)

**Status:** PENDING
**Type:** feature
**Priority:** High
**Depends on:** 020
**Labels:** mail, smtp, jmap, send, safety, axi
**Phase:** Wave 10 (embedded mail send)
**Design reference:** [DN-mail-access.md](../research/DN-mail-access.md) §Decision 7 (sending) · §Decision 2 (transport hybrid) · §Decision 3 (credentials) · §Decision 4 (secret resolver)
**Author:** Antony John · **Co-author:** Vidushi (orchestrator — office-assistant)

## Context

CR-OA-020 embedded the **read** side of mail (`mail-search`/`mail-get`/adapters); **sending was a documented
non-goal** and DN-mail-access §Decision 3 deliberately scoped a **read-only** Fastmail token. But the unified
skill's **Support domain requires draft-then-confirm outbound mail** (RMA / claims / service — "reply from the
buying alias the vendor knows, **never auto-send**"), which today routes through the **harness mail MCP** —
leaving DN §Decision 1's "embed mail to supersede the MCP" half-done and non-portable. This CR embeds sending
in `voa` per DN §Decision 7, with **draft-then-confirm enforced at the engine level** (never auto-send).

## Scope

### §S1 Send transport + `send` capability
A `MailSender` capability on the adapters, reusing the §S4 secret resolver + XOAUTH2: **Gmail/Yahoo** send via
stdlib **SMTP submission** (`smtplib`, STARTTLS :587 / SSL :465, authenticated by the IMAP app-password or
XOAUTH2); **Fastmail** sends via **JMAP `EmailSubmission/set`** (app-password SMTP as fallback). Send is
**opt-in per account** — `mail-auth` records a **`send` capability flag** (a send-capable credential), and the
send verbs refuse an account lacking it. Adapter methods: `create_draft(raw_rfc822, folder="Drafts") -> draft_id`
and `send_draft(draft_id) -> message_id`.

### §S2 RFC 5322 composition + reply threading + From-identity
A `compose(from_addr, to, subject, body, cc=None, in_reply_to=None, references=None, attachments=None) -> bytes`
builds a valid RFC 5322 message (`email.message.EmailMessage`). A **reply** sets `In-Reply-To` to the source
`Message-ID` and `References` to the source chain (from a `mail-get`-fetched `Message`). The **From** is a
**validated identity** — the account address or a configured **Fastmail masked alias** (JMAP `Identity/get` /
an alias list on the account entry); an unknown From is refused.

### §S3 Draft-then-confirm verbs — NEVER auto-send
`voa` has **no code path that sends without an explicit, identified `mail-send`**:
- **`mail-draft`** `--account --from --to --subject --body [--cc] [--attach <path>] [--case <id>]` — composes
  (§S2) and **saves a real draft** to the account's Drafts (JMAP `Email/set` with `$draft` / IMAP `APPEND`);
  emits a TOON status with the **draft id**; performs **no network send**.
- **`mail-send`** `--account --draft <draft-id>` — dispatches **only that identified draft** (JMAP
  `EmailSubmission/set` / SMTP); emits the sent **message id**.
- **`mail-reply`** `--account --uid <src-uid> --from --body [--attach] [--case]` — fetches the source, composes
  a **threaded** reply (§S2), saves it as a draft (same as `mail-draft`).

### §S4 Guards — verified recipient + From identity
- **Verified-recipient guard:** `mail-draft`/`mail-reply` to a recipient that is **not a verified `contact`**
  emit a structured error + exit 1, unless `--force`.
- **From-identity validation:** a `--from` not among the account's identities/aliases → structured error + exit 1.

### §S5 Store linkage — the correspondence trail
`mail-draft`/`mail-reply` accept a `--case`/`--invoice`/`--warranty`/`--order` FK. On `mail-send` of a linked
draft, the engine records the sent message as a **`document`** on the linked row and **resolves/appends the
relevant `action`** (e.g. a case's `raise-ticket`) — so support mail is part of the tracked lifecycle.

### §S6 Attachments
`mail-draft --attach <path>` attaches a file (e.g. a `documents/<acct>/<vendor>/*.pdf` invoice) — the composed
message becomes `multipart` carrying the attachment (filename + bytes).

All verbs are AXI-conformant (CR-OA-017): TOON status envelope, `--json`, structured errors to stdout, exit codes.

## Acceptance criteria

Adapter/verb tests run against **fakes** (a fake SMTP server / a fake JMAP `EmailSubmission` endpoint / a fake
Drafts store) — **no live sending in the suite**. Live-send verification is a documented **manual** step.

### §S1
- [ ] Against a fake SMTP server, the Gmail/Yahoo adapter's `send_draft` connects with submission + STARTTLS, authenticates with the account credential, and issues exactly one `sendmail`; against a fake JMAP endpoint, the Fastmail adapter issues one `EmailSubmission/set` referencing the draft's email id.
- [ ] A `send`-verb call against an account whose registry entry lacks the `send` capability flag exits 1 with a structured error (grep the error for `send`); a send-capable account proceeds.

### §S2
- [ ] `compose(from_addr="me@x", to="v@y", subject="S", body="B")` returns bytes whose parsed headers are `From: me@x`, `To: v@y`, `Subject: S`; a reply built with `in_reply_to="<m1@y>"` sets `In-Reply-To: <m1@y>` and includes `<m1@y>` in `References`.
- [ ] `compose(from_addr=<not-an-identity>, …)` (or the verb path) raises/exits with a structured error naming the invalid From.
- [ ] **No personal data in the client (DN Consequences invariant):** the From/recipient/alias values come only from account config + verb args — a grep asserts the send path in `vidushi_oa/` hardcodes no real mailbox address or masked alias.

### §S3
- [ ] `mail-draft` against a fake adapter records exactly one draft-save (an `APPEND`/`Email/set $draft`) and **zero** sends (the fake's send-count is 0), and returns a `draft` id in its TOON status.
- [ ] `mail-send --draft <id>` triggers exactly one `send_draft(<id>)` on the adapter and returns a message id.
- [ ] **No-auto-send invariant (mechanically auditable):** a grep shows `send_draft`/`EmailSubmission/set`/`sendmail` invoked from **only** `cmd_mail_send` in `vidushi_oa/_cli.py` (no other verb calls a send path).
- [ ] **Caller-existence:** `voa --help` lists `mail-draft`/`mail-send`/`mail-reply`, and each is wired via a non-test `set_defaults` caller (grep ≥1 each).

### §S4
- [ ] `mail-draft --to <address-not-in-contacts>` exits 1 with a structured error naming the unverified recipient; `--force` lets it through (draft saved).
- [ ] `mail-draft --from <address-not-an-account-identity>` exits 1 with a structured error.

### §S5
- [ ] `mail-send` of a draft created with `--case case_x` records a `document` on `case_x` (a `voa get cases case_x` shows the message reference) and resolves/appends the mapped correspondence `action`.

### §S6
- [ ] `mail-draft --attach <a .pdf under documents/>` yields a `multipart` draft whose attachment part carries that filename and the file's byte length (asserted via the fake Drafts store's captured raw message).

## Estimated size
L–XL — a send subsystem: two transports (SMTP + JMAP `EmailSubmission`), RFC 5322 composition + threading +
alias identity, three draft-then-confirm verbs, the verified-recipient/identity guards, store linkage, and
attachments.

## Risk
Sending is **irreversible / high-stakes** — mitigated by the engine-level draft-then-confirm gate (no send
path but `mail-send` on an identified draft, mechanically audited), the verified-recipient guard, and
fakes-only tests (no live send in the suite). **Credential scope** — send needs a send-capable credential (a
Fastmail `submission`-scoped token or an app-password); `mail-auth` records the opt-in `send` flag, read-only
accounts stay read-only. **From spoofing** — From validated against the account's identities. **Provider
drift** — SMTP is stable; JMAP submission is native to Fastmail.

## Non-goals
Bulk / mass send; a send daemon or IDLE/queue; calendar invites; rich **HTML** composition (plain-text body +
attachments in v1); **auto-send of any kind** (permanently out — draft-then-confirm is the contract); the
skill revision that teaches the Support domain to use `voa mail-draft`/`mail-send` (a follow-up docs CR).
