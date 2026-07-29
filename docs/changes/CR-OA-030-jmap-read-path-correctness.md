# CR-OA-030 — JMAP read-path correctness (drop `deliveredTo`, surface method errors, expose `uid`)

**Status:** COMPLETED (shipped 2026-07-29 on develop)
**Type:** bugfix
**Priority:** Critical
**Depends on:** 020, 026
**Labels:** mail, jmap, read-path, axi, blocking
**Phase:** Wave 11 (1.1.2 read-path patch)
**Design reference:** [DN-mail-access.md](../research/DN-mail-access.md) §Decision 2 (transport hybrid + its 2026-07-29 revision) · §Decision 5 (`mail-*` verbs) · [DN-mail-e2e-emulator-testing.md](../research/DN-mail-e2e-emulator-testing.md)
**Author:** Antony John · **Co-author:** Vidushi (orchestrator — office-assistant)

## Context

`voa mail-search` returns `count: 0` for **every** query on a Fastmail/JMAP account while the same
queries work on Gmail — reported from the field against the installed 1.1.1, and reproduced end-to-end
against a compliant JMAP server (`tests/e2e/test_mail_read_e2e.py`).

One defect explains it. `_EMAIL_PROPERTIES` (`vidushi_oa/mail/jmap.py`) requests **`deliveredTo`**, which
is **not an RFC 8621 `Email` property** (it was introduced for the masked-alias correlation key). A
compliant server rejects the projection, so the batched search's `Email/query` succeeds while its paired
`Email/get` answers a method-level error *inside* HTTP 200:

```
["Email/query", {…, "ids": ["haaaaacy", "faaaaabi"]}, "0"],
["error", {"type": "invalidArguments", "description": "Invalid property deliveredTo"}, "1"]
```

`_parse()` scans `methodResponses` only for an `Email/get` **response** and returns `[]` when it finds
none — so the error is swallowed to `count: 0` at **exit 0**, an AXI #6 violation that reads to an agent
as "no mail matched". The same projection is used by `fetch_message`, so `mail-get` fails identically.
Two further gaps surfaced in the same pass: the JMAP `_build_message` never sets `uid` (so CR-OA-026's
`uid` exposure never reached the JMAP adapter and `mail-get` is unusable on Fastmail regardless), and the
CR-OA-020 fake transport returns canned payloads *containing* `deliveredTo` — proving the adapter can
parse the field, never that a server accepts it in a request.

## Scope

### §S1 Drop the non-conformant `deliveredTo` projection
Remove `"deliveredTo"` from `_EMAIL_PROPERTIES` so the `Email/get` projection contains only RFC 8621
`Email` properties. The masked-alias correlation key is retained by requesting the delivered-to **header**
instead — a conformant header projection (`header:Delivered-To:asText:all`) — and `_build_message` keeps
populating `Message.delivered_to` from it. Where the header is absent the field is `""` (today's
behaviour for a message without it); the alias trick degrades, it does not error.

**Surfaces (verified 2026-07-29):** `vidushi_oa/mail/jmap.py` `_EMAIL_PROPERTIES` (~line 28),
`_build_message` (~line 419).

### §S2 Surface method-level JMAP errors instead of collapsing to an empty result
`search()` must not report success when the server reported an error. A JMAP response carrying a
top-level `["error", {...}, callId]` methodResponse, **or** lacking the `Email/get` response its
back-reference required, raises a structured `RuntimeError` naming the server's `type` and
`description`; `cmd_mail_search` renders it as an AXI #6 structured error with a **non-zero exit**. The
existing inline `["error", …]` handling in `_created_id` / the ids helper is factored into one reusable
check so every method path uses the same rule. A genuinely empty `Email/query` (`ids: []`) stays a clean
`count: 0` at exit 0 — an empty result and a failed query are no longer indistinguishable.

**Surfaces:** `vidushi_oa/mail/jmap.py` `search()` (~line 390), `_parse()` (~line 410), `_created_id`
(~line 58); `vidushi_oa/_cli.py` `cmd_mail_search`.

### §S3 Expose `uid` on JMAP rows (CR-OA-026 parity)
`JmapAdapter._build_message` sets `Message.uid` to the JMAP `Email` id, so a `mail-search` row on a
Fastmail account carries a non-null `uid` that `mail-get --account <a> --uid <uid>` resolves — the parity
CR-OA-026 established for IMAP and explicitly scoped out of JMAP.

### §S4 Read-path regression contracts
The read-path E2E (`tests/e2e/test_mail_read_e2e.py`, local tier) is the regression guard: its three
`xfail(strict=True)` contracts flip to passing tests with the markers removed, and the
`deliveredTo`-still-present guard is deleted with the property. Fakes-based coverage is corrected in the
same pass so it can no longer assert a projection a real server rejects.

## Acceptance criteria

### §S1
- [x] `_EMAIL_PROPERTIES` contains no `deliveredTo` entry; every remaining entry is an RFC 8621 `Email`
      property (`id`, `threadId`, `messageId`, `subject`, `from`, `to`, `receivedAt`) plus the
      delivered-to **header** projection (`header:Delivered-To:asText:all`).
- [x] `_build_message` populates `Message.delivered_to` from that header projection when present, and
      `""` when absent (no raise).
- [x] Against the E2E Stalwart `fastmail` profile, an `Email/get` issued with the new projection returns
      an `Email/get` response (no `["error", …]` methodResponse).

### §S2
- [x] Given a response whose `methodResponses` carry `["error", {"type": "invalidArguments", …}, "1"]`,
      `JmapAdapter.search(...)` raises (does **not** return `[]`), and the raised message contains both
      the server's `type` and its `description`.
- [x] Given a response with an `Email/query` result but **no** `Email/get` response, `search()` raises
      naming the missing `Email/get` — it does not return `[]`.
- [x] Given an `Email/query` answering `ids: []` with a matching empty `Email/get`, `search()` returns
      `[]` and `voa mail-search` prints `count: 0` at **exit 0** (a legitimate empty stays clean).
- [x] `voa mail-search '<q>' --accounts <jmap-account>` against a server that rejects the request exits
      **non-zero** with a structured error payload (no traceback, per AXI #6).
- [x] A grep shows one reusable method-level error check used by `search`/`_parse` and the pre-existing
      `_created_id` path (no duplicated `["error", …]` literal handling).

### §S3
- [x] For a JMAP-sourced row, `Message.uid` equals the JMAP `Email` id and is non-null.
- [x] End-to-end against the `fastmail` profile: a `uid` taken from a `voa mail-search --json` row
      resolves via `voa mail-get --account <a> --uid <uid>` (exit 0).

### §S4
- [x] `tests/e2e/test_mail_read_e2e.py::test_mail_search_returns_the_seeded_amazon_rows`,
      `::test_mail_get_opens_a_seeded_message_by_jmap_id`, and
      `::test_smtp_roundtrip_message_is_findable_via_mail_search` pass with **no** `xfail` marker.
- [x] `::test_deliveredTo_property_is_rejected_by_the_compliant_jmap_server` (the property-still-present
      guard) is removed, and `::test_search_swallows_the_email_get_error_to_empty` /
      `::test_forced_query_error_is_also_swallowed_to_empty` are inverted to assert the error is now
      **raised**.
- [x] `make e2e` is green; the default runner still deselects the whole e2e tier
      (`pytest tests/ -q --collect-only` collects zero `tests/e2e` tests).
- [x] No fakes-based test asserts `deliveredTo` in a request projection (grep over `tests/`).

## Estimated size
S–M — a projection change, one error-surfacing helper threaded through the JMAP read path, a `uid`
assignment, and the regression-contract flips.

## Risk
The masked-alias correlation key (DN §Decision 2) changes mechanism from a non-conformant property to a
header projection — if a provider omits `Delivered-To`, alias correlation degrades to `""` (mitigated:
it already degrades that way for messages lacking the header, and the E2E asserts the non-error path).
Surfacing previously-swallowed errors will make **existing** latent server rejections visible as
failures rather than empty results — that is the intent, but it can turn a silent `count: 0` into a
loud error for any other non-conformant request we still make.

## Non-goals
Portable-query grammar translation (CR-OA-031); returning message **bodies**/`to`/`cc` from `mail-get`
(CR-OA-032); Gmail/IMAP-side search changes; the Mongo validator migration (CR-OA-033).
