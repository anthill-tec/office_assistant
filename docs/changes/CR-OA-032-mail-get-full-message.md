# CR-OA-032 — `mail-get` returns a full message (decoded body, `to`/`cc`, decoded subject)

**Status:** PENDING
**Type:** feature
**Priority:** High
**Depends on:** 026, 030
**Labels:** mail, mail-get, body, mime, axi
**Phase:** Wave 11 (1.1.2 read-path patch)
**Design reference:** [DN-mail-access.md](../research/DN-mail-access.md) §Decision 5 (`mail-*` verbs, token-saving pre-processing) · [DN-agent-interface-toon.md](../research/DN-agent-interface-toon.md) (AXI #2 minimal fields, #3 truncation)
**Author:** Antony John · **Co-author:** Vidushi (orchestrator — office-assistant)

## Context

CR-OA-026 exposed a `uid` on `mail-search` rows so an agent could open one message. It cannot: `mail-get`
returns the **envelope only**. `_mail_row` projects `id/uid/account/source_tag/subject/sender/date` — no
body, no `to`, no `cc` — and a message body is fetched *only* by `fetch_html_body`, documented as
"consumed in-engine only" for `mail-extract` and deliberately never surfaced. Reported from the field:
opening a shipping mail to read the order number / amount / AWB / ETA is still impossible through `voa`,
so the uid exposure did not close the gap it was meant to close. The same call returns `subject`
**RFC 2047-encoded** (`=?UTF-8?B?U2hpcHBlZDog…?=`) rather than decoded, pushing MIME decoding onto every
caller.

The original no-body decision was about **token frugality** on the *search* path — the merge/de-dup/TOON
pass exists so an agent never handles raw email. That rationale holds for `mail-search` (many rows) and
does **not** hold for `mail-get`, whose entire purpose is "open exactly one identified message". This CR
keeps bodies off the search path and makes `mail-get` the bounded, AXI-conformant escape hatch it was
always meant to be.

## Scope

### §S1 `mail-get` returns the message body
`voa mail-get --account <a> --uid <uid>` returns a **decoded plain-text body**: the `text/plain` part
when present, else the `text/html` part stripped to text. Transfer-encodings (base64,
quoted-printable) are decoded and the declared charset is honoured, falling back to UTF-8 with
replacement rather than raising. Attachments are **never** inlined — they are listed by
filename + size only. Both adapters serve it: JMAP via its body-property fetch, IMAP via its existing
body retrieval.

**Surfaces (verified 2026-07-29):** `vidushi_oa/_cli.py` `_mail_row` (~line 799), `cmd_mail_get`
(~line 903); `vidushi_oa/mail/imap.py` `fetch_message`/`fetch_html_body` (~lines 165/387);
`vidushi_oa/mail/jmap.py` `fetch_message` (~line 477).

**Folded in from CR-OA-030's VERIFY (2026-07-29) — the fetch path must not misattribute a server error.**
`vidushi_oa/mail/jmap.py` `_email_get_list` (~line 487), used by `fetch_message` and `fetch_html_body`,
does **not** route through the shared `_raise_for_method_error` that CR-OA-030 introduced. A method-level
`Email/get` error (auth/permission/invalid-argument) therefore returns `[]` → `fetch_message` returns
`None` → `cmd_mail_get` reports `"message not found"`, losing the server's real `type`/`description`. It
is not a false success (still exit 1, still structured) but it **misattributes the cause**, which is the
same defect class CR-OA-030 removed from `search`. Since this CR rewrites `fetch_message` anyway, it
routes that path through the shared check too. Also correct the stale docstring at `_cli.py` ~lines
907–908, which still claims `JmapAdapter` raises `NotImplementedError` for `mail-get` (superseded by
CR-OA-028's real `fetch_message`).

### §S2 Full recipient envelope + decoded headers
The `mail-get` payload carries `to` and `cc` (currently absent), and every human-readable header —
`subject`, and the display-name portions of `sender`/`to`/`cc` — is **RFC 2047-decoded** to UTF-8 before
it leaves the engine. Decoding is applied on the shared projection path so `mail-search` rows get
decoded subjects too (a display fix, not a payload-size change).

### §S3 AXI conformance — bounded by default
`mail-get` stays AXI-conformant: the body is **truncated by default** per AXI #3 with the standard
`…(+N chars)` marker, and `--full` returns it untruncated; `--json` remains a bare object. The verb
carries an AXI #9 `next[]` (e.g. the `mail-extract` call for that same message). **`mail-search` rows do
not gain a body** — the token-frugal search contract is unchanged.

### §S4 Body retrieval stays single-purpose
`mail-extract` continues to use its own HTML retrieval; this CR does not route extraction through
`mail-get`. The bodies exposed here are for agent reading, and the "never surface raw email in bulk"
principle is preserved by §S3's truncation + the search-path exclusion.

## Acceptance criteria

### §S1
- [ ] `voa mail-get --account <a> --uid <uid>` returns a payload containing a non-empty `body` for a
      seeded `text/plain` message, and the body text matches the seeded content.
- [ ] For an HTML-only message the returned `body` is the HTML stripped to text (no tags in the output).
- [ ] A base64 / quoted-printable encoded body is returned **decoded** (asserted against a seeded message
      whose raw part is encoded).
- [ ] A message with an attachment returns the attachment's `filename` and size and **no** attachment
      bytes in the payload.
- [ ] Both adapters satisfy the above: the assertions run against the E2E `fastmail` (JMAP) profile **and**
      an IMAP profile.
- [ ] A method-level `Email/get` error on the fetch path (e.g. `["error", {"type": "forbidden",
      "description": …}, …]`) makes `voa mail-get` report **that server error's `type`/`description`** in
      its structured payload — **not** `"message not found"` — proving `_email_get_list`/`fetch_message`
      route through CR-OA-030's shared `_raise_for_method_error`.
- [ ] The `cmd_mail_get` docstring no longer claims `JmapAdapter` raises `NotImplementedError`
      (grep finds no such claim).

### §S2
- [ ] The `mail-get` payload contains `to` and `cc` fields populated from the message (empty list when
      absent).
- [ ] A seeded message whose raw `Subject:` is `=?UTF-8?B?…?=` returns a **decoded** UTF-8 `subject`
      (a grep of the output for `=?UTF-8?` finds nothing).
- [ ] `mail-search` rows carry the same decoded subject.

### §S3
- [ ] A body longer than the AXI #3 threshold is truncated by default and carries the `…(+N chars)`
      marker; the same call with `--full` returns the untruncated body.
- [ ] `voa mail-get --json` returns a **bare object** with no `next`/`tally` wrapper (AXI decision-B), while
      the default TOON output carries a runnable `next[]`.
- [ ] `mail-search` output contains **no** `body` field for any row (mechanically grepped).

### §S4
- [ ] `mail-extract` behaviour is unchanged (its existing tests pass untouched).
- [ ] **Caller-existence:** `voa mail-get --help` documents `--full` and the body/`to`/`cc` fields.

## Estimated size
M — body retrieval + MIME/charset decoding on two adapters, projection widening, RFC 2047 decoding, and
the AXI truncation/`next[]` wiring.

## Risk
Body retrieval increases per-call payload size and touches PII — mitigated by AXI #3 truncation, the
search-path exclusion, and never inlining attachments. Malformed MIME/charset must degrade (replacement
chars) rather than raise, or `mail-get` becomes fragile on real-world mail; the encoded-body ACs are the
guard. HTML-to-text stripping is heuristic and may render imperfectly for heavily-templated marketing
mail — acceptable for agent reading, and `mail-extract` remains the structured path.

## Non-goals
Routing `mail-extract` through `mail-get`; returning attachment **contents** or saving them; bodies on
`mail-search` rows; rendering HTML faithfully; threading/conversation assembly.
