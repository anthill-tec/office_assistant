# CR-OA-026 — `mail-search` omits the IMAP `uid` (+ account) → `mail-get` unusable

**Status:** COMPLETED (shipped 2026-07-28 on 1.1.0)
**Type:** bugfix
**Priority:** High
**Depends on:** 020
**Labels:** mail, imap, mail-get, axi, bug
**Phase:** Wave 10 (embedded mail send)
**Design reference:** [DN-mail-access.md](../research/DN-mail-access.md) §Decision 5 (`mail-*` verbs)
**Author:** Antony John · **Co-author:** Vidushi (orchestrator — office-assistant)

## Context

`_mail_row()` (`vidushi_oa/_cli.py:737–740`) projects a `Message` to only
`{id, source_tag, subject, sender, date}` — where `id` is the **RFC Message-ID**. It **drops `msg.uid`**
(the numeric IMAP UID) **and `msg.account`**, even though both are already populated on the `Message`
(`base.py:33`, captured in `imap.py:_build_message`). `mail-get` requires `--account <name> --uid <numeric>`;
with no `uid`/`account` in the search output, the only handle a caller has is the Message-ID, and passing that
as `--uid` produces a malformed `UID FETCH` → `BAD Could not parse command`. **Consequence:** no message body
can be opened from a search result — the reason delivery/tracking mail had to be read in the browser instead
of through `voa`. Affects default, `--full`, and `--json` output.

## Scope

### §S1 Expose `uid` + `account` on every mail-search row
`_mail_row()` includes **`uid` (`msg.uid`)** and **`account` (`msg.account`)** alongside the existing fields,
so each search row is directly consumable by `mail-get --account <account> --uid <uid>`. Present in **all
three** output modes (default TOON, `--full`, `--json`).

### §S2 AXI-conformant response (CR-OA-017)
The fix's response conforms to AXI, not merely carrying the field:
- **Minimal default fields (#2):** `uid` + `account` are part of the **minimal default projection**, not
  `--full`-only — the agent must be able to chain `mail-get` from the default TOON envelope without asking for
  `--full`.
- **Contextual next-command hint (#9):** `mail-search`'s `next[]` emits a **runnable**
  `mail-get --account <account> --uid <uid>` built from the first row's real values (values come from the row
  at runtime — the no-personal-data invariant holds: nothing hardcoded).
- **Envelope + empty state (#1/#5):** rows stay inside the standard `{count, results, next}` TOON envelope; a
  no-hits search returns the definitive empty state, not a bare list.

## Acceptance criteria

### §S1
- [ ] `_mail_row(msg)` returns a dict containing `uid == msg.uid` and `account == msg.account`, in addition to `id`/`source_tag`/`subject`/`sender`/`date`.
- [ ] A `mail-search` run (fake adapter returning a `Message` with `uid="42"`, `account="fastmail"`) emits rows carrying `uid: "42"` and `account: "fastmail"` in default TOON, `--full`, **and** `--json`.
- [ ] **Integration (round-trip):** the `(account, uid)` taken from a `mail-search` row, passed to `mail-get --account <account> --uid <uid>` against the same fake adapter, resolves the message (no `UID FETCH` malformation) — asserting a search result is now directly openable.

### §S2 (AXI conformance)
- [ ] `uid` + `account` appear in the **default** TOON output (no `--full`, no `--fields`) — asserted on the minimal-default projection, not only under `--full`.
- [ ] `mail-search`'s `next[]` contains a runnable `mail-get --account <account> --uid <uid>` string built from the first result row's actual `account`/`uid` (assert the exact interpolated command); with zero hits, `next[]` falls back to the search-refinement hint and `count` is `0` (definitive empty state).

## Estimated size
XS — add two already-present fields to the row projection + a round-trip test.

## Risk
Minimal — additive fields on the output row; no adapter or fetch change. The `uid` is per-account and per
mailbox session, but `mail-get` already takes `--account`, so the pair is sufficient.

## Non-goals
Making `mail-get` additionally accept a **Message-ID** (a nice-to-have the fix makes unnecessary — a
Message-ID→UID resolution would require an extra search; deferred). Any change to the fetch/threading path or
the JMAP adapter.
