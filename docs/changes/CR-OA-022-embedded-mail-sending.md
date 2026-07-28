# CR-OA-022 — Embedded mail *sending* in `voa` (draft-then-confirm, per-mailbox identity, store-linked)

**Status:** COMPLETED (shipped 2026-07-28 on 1.1.0)
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
stdlib **SMTP submission** (`smtplib`, STARTTLS :587 / SSL :465, authenticated by the IMAP app-password with
`SMTP.login`, or — for a Workspace account that carries no password — by the **`XOAUTH2` SASL mechanism**
(`SMTP.auth`) over the same refreshed access token the IMAP side uses); **Fastmail** sends via **JMAP
`EmailSubmission/set`** (app-password SMTP as fallback). Send is
**opt-in per account** — `mail-auth` records a **`send` capability flag** (a send-capable credential), and the
send verbs refuse an account lacking it. Adapter methods: `create_draft(raw_rfc822) -> draft_id`
and `send_draft(draft_id) -> message_id`. The IMAP implementations of both also accept an optional `folder`, defaulting
to the account's own `\Drafts` **special-use** mailbox (RFC 6154, resolved from `LIST`) rather than a literal
`"Drafts"`; JMAP takes no `folder` at all — it resolves the `drafts`-role mailbox itself, so a folder name
would have nothing to bind to.

### §S2 RFC 5322 composition + reply threading + From-identity
A `compose(from_addr, to, subject, body, cc=None, in_reply_to=None, references=None, attachments=None) -> bytes`
builds a valid RFC 5322 message (`email.message.EmailMessage`), always stamping the §3.6.4 originator headers
**`Date`** and a **`Message-ID`** scoped to the From domain (`EmailMessage` mints neither, and without them two
identical drafts serialize to identical bytes a content-addressed blob store collapses into one), and
serializing under `email.policy.SMTP` so the bytes carry the **CRLF** line endings RFC 5322 §2.1 mandates
(the default policy emits bare LF, which an IMAP `APPEND` literal and a `message/rfc822` blob upload both
transmit verbatim). A **reply** sets `In-Reply-To` to the source
`Message-ID` and `References` to the source chain (from a `mail-get`-fetched `Message`). The **From** is a
**validated identity** — the account address or a configured **Fastmail masked alias** (JMAP `Identity/get` /
an alias list on the account entry); an unknown From is refused.

### §S3 Draft-then-confirm verbs — NEVER auto-send
`voa` has **no code path that sends without an explicit, identified `mail-send`**:
- **`mail-draft`** `--account --from --to --subject --body [--cc] [--attach <path>] [--case <id>]` — composes
  (§S2) and **saves a real draft** to the account's Drafts — carrying the composed content: JMAP uploads
  the literal RFC822 bytes as a blob to the session `uploadUrl` and `Email/import`s that blob into the
  `drafts`-role mailbox with the `$draft` keyword; IMAP `APPEND`s the bytes with `\Draft` to the `\Drafts`
  special-use mailbox resolved from `LIST` (never the literal `"Drafts"` — that is `[Gmail]/Drafts` on Gmail
  and `Draft` on Yahoo) — emits a TOON status with the **draft id**; performs **no network send**.
- **`mail-send`** `--account --draft <draft-id>` — dispatches **only that identified draft** (JMAP
  `EmailSubmission/set` / SMTP); emits the sent **message id**. The JMAP submission carries an
  `onSuccessUpdateEmail` patch clearing `$draft` and moving the message into the `sent`-role mailbox, so a
  sent message stops being a draft and Sent holds the record of the correspondence. The IMAP path reaches
  the same end state after its `sendmail`: it `APPEND`s the sent bytes `\Seen` to the `\Sent` special-use
  mailbox (skipped for providers that file their own copy — Gmail) and retires the Drafts copy
  (`-FLAGS (\Draft)`, `+FLAGS (\Deleted)`, UID `EXPUNGE`). The Drafts copy is destroyed **only** once a Sent
  copy is confirmed to exist — a tagged-`OK` `APPEND`, or a provider that files its own; no `\Sent` mailbox,
  a refused `APPEND` or a refused `STORE` leaves the draft in place, so a sent message can never end up in
  neither folder. All of it runs after delivery, so none of it can fail a sent message.
- **`mail-reply`** `--account --uid <src-uid> --from --body [--attach] [--case]` — fetches the source, composes
  a **threaded** reply (§S2), saves it as a draft (same as `mail-draft`).

### §S4 Guards — verified recipient + From identity
- **Verified-recipient guard:** `mail-draft`/`mail-reply` to a recipient that is **not a verified `contact`**
  emit a structured error + exit 1, unless `--force`. The guard covers **every address the message will be
  submitted to** — `--cc` as well as `--to`, and each address of a comma-separated, display-name-carrying
  header value parsed individually, because `send_draft` builds its RCPT list from all of `To` + `Cc`.
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
- [ ] The XOAUTH2 (Workspace) adapter authenticates SMTP with `AUTH XOAUTH2` — never an empty-password `LOGIN` — carrying the *unencoded* `user=…\x01auth=Bearer …\x01\x01` SASL payload (`smtplib` base64-encodes it) built from the *same* access token the IMAP side minted (one token per adapter); the app-password adapter still authenticates with `LOGIN`.
- [ ] The TLS channel is re-greeted (`EHLO`) after `STARTTLS` before either authentication — RFC 3207 discards the pre-TLS greeting, and `SMTP.auth` (unlike `SMTP.login`) does not re-greet, so an un-greeted `AUTH` is answered `503 EHLO/HELO first` and silently swallowed, failing later at `MAIL FROM`.
- [ ] The submission connection is closed (`QUIT`) on every path — delivered, or failed at STARTTLS/authentication/`sendmail` — and a `QUIT` that itself fails never turns a delivered send into an error.

### §S2
- [ ] `compose(from_addr="me@x", to="v@y", subject="S", body="B")` returns bytes whose parsed headers are `From: me@x`, `To: v@y`, `Subject: S`; a reply built with `in_reply_to="<m1@y>"` sets `In-Reply-To: <m1@y>` and includes `<m1@y>` in `References`.
- [ ] Every composed message carries a `Date` and a `Message-ID` whose domain comes from the From address (never the local host), so two identical `compose(...)` calls are not byte-identical.
- [ ] Every line of the serialized bytes ends in CRLF (header/body separator included) — no bare LF reaches an IMAP `APPEND` literal or a `message/rfc822` blob upload.
- [ ] `compose(from_addr=<not-an-identity>, …)` (or the verb path) raises/exits with a structured error naming the invalid From.
- [ ] **No personal data in the client (DN Consequences invariant):** the From/recipient/alias values come only from account config + verb args — a grep asserts the send path in `vidushi_oa/` hardcodes no real mailbox address or masked alias.

### §S3
- [ ] `mail-draft` against a fake adapter records exactly one draft-save (an IMAP `APPEND`, or a JMAP blob upload of the literal RFC822 bytes followed by one `Email/import` that references BOTH the returned `blobId` and the resolved `drafts` mailbox id, with `$draft` set) and **zero** sends (the fake's send-count is 0), and returns a `draft` id in its TOON status.
- [ ] The JMAP draft path never reports a draft it did not create: a blob upload answering any 2xx (incl. `201 Created`) succeeds, while a session with no `uploadUrl`, an upload returning no `blobId`, a failed or empty `Mailbox/query` (whose own server error is surfaced verbatim, not re-labelled "no Drafts mailbox"), or an `Email/import` answering `notCreated`/`["error", …]` each raise a structured error instead of an empty draft id. `notCreated` admits no exception — `alreadyExists` included: every composed message carries a unique `Message-ID`, so each import is expected to create, and a rejection never resolves to another message's id.
- [ ] `mail-send --draft <id>` triggers exactly one `send_draft(<id>)` on the adapter and returns a message id. The JMAP `EmailSubmission/set` carries an `onSuccessUpdateEmail` patch clearing `keywords/$draft` and setting `mailboxIds` to the resolved `sent`-role mailbox (the move is skipped, but `$draft` still cleared and the submission still issued, when the account has no Sent mailbox **or** its `Mailbox/query` fails — at the method level *or* the transport level, i.e. an `HTTPError`/`OSError` from a 4xx/5xx and a `ValueError` from a non-JSON 2xx body; a submission needs no mailbox, so the lookup never blocks a send).
- [ ] The IMAP send path reaches the same end state: after the one `sendmail`, the sent bytes are `APPEND`ed `\Seen` to the `\Sent` special-use mailbox resolved from `LIST` (quoted or bare-atom name, never the hierarchy delimiter; no `APPEND` for Gmail, which files its own copy) and the Drafts copy is retired (`-FLAGS (\Draft)`, `+FLAGS (\Deleted)`, UID `EXPUNGE` of that UID only). The retire is gated on a confirmed Sent copy: an account advertising no `\Sent` mailbox, an `APPEND`/`STORE` answering a tagged `NO`, or a raised bookkeeping failure leaves the Drafts copy untouched (never expunged). Every one of those still returns the message id — a delivered message is never reported as a failed send.
- [ ] An IMAP `APPEND` that answers a tagged `NO` (which `imaplib` does not raise on) fails `create_draft` structurally, mirroring the JMAP `notCreated` handling — never a `drafted` status carrying the server's error text as the draft id.
- [ ] The IMAP drafting verbs address the `\Drafts` **special-use** mailbox resolved from `LIST` — attribute first, else a provider name the server actually listed (`[Gmail]/Drafts`, `Draft`) — and `send_draft` fetches the draft back from that same resolved mailbox; an account advertising neither is a structural error, and a `LIST` answering a tagged `NO` raises instead of being cached as a missing Sent/Drafts mailbox for the rest of the process.
- [ ] **No-auto-send invariant (mechanically auditable):** a grep shows `send_draft`/`EmailSubmission/set`/`sendmail` invoked from **only** `cmd_mail_send` in `vidushi_oa/_cli.py` (no other verb calls a send path).
- [ ] **Caller-existence:** `voa --help` lists `mail-draft`/`mail-send`/`mail-reply`, and each is wired via a non-test `set_defaults` caller (grep ≥1 each).

### §S4
- [ ] `mail-draft --to <address-not-in-contacts>` exits 1 with a structured error naming the unverified recipient; `--force` lets it through (draft saved).
- [ ] The same holds for `--cc` (an unverified Cc exits 1 naming that address, a verified one saves without `--force`), and for **every** address of a multi-address recipient value — a header naming a verified contact first and an unverified address second is refused, naming the second.
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
