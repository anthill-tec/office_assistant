# CR-OA-025 — Gmail `X-GM-RAW` search mis-quotes embedded phrases (malformed IMAP SEARCH)

**Status:** PENDING
**Type:** bugfix
**Priority:** High
**Depends on:** 020
**Labels:** mail, imap, gmail, search, bug
**Phase:** Wave 10 (embedded mail send)
**Design reference:** [DN-mail-access.md](../research/DN-mail-access.md) §Decision 2 (transport hybrid — Gmail `X-GM-RAW`)
**Author:** Antony John · **Co-author:** Vidushi (orchestrator — office-assistant)

## Context

`GmailImapAdapter.search()` builds the Gmail server-side search command as
`conn.uid("SEARCH", "X-GM-RAW", '"%s"' % query)` (`vidushi_oa/mail/imap.py:122`). It wraps the query in
double-quotes but **does not escape embedded `"` (or `\`)**. A query containing a **quoted phrase** — e.g.
`category:purchases "out for delivery"` — therefore becomes an **unbalanced IMAP quoted-string**, and Gmail
rejects the `SEARCH` as malformed. Observed in local Phase-1 use: individual qualifiers, `OR`, and
parentheses all work; **only quoted phrases break** (they are the sole construct that introduces a `"` into
the argument). Yahoo (`imap.py:140`) and the base adapter (`imap.py:54`) pass already-structured RFC 3501
queries and are unaffected.

## Scope

### §S1 Correctly quote/escape the `X-GM-RAW` argument
`GmailImapAdapter.search()` sends the `X-GM-RAW` value as a **valid IMAP argument** for any query, including
one with embedded quotes: escape `\` → `\\` and `"` → `\"` within the RFC 3501 quoted-string (or send the
value as an IMAP literal `{N}`). Compound queries mixing qualifiers, `OR`, parentheses, **and** quoted
phrases translate to a well-formed `UID SEARCH X-GM-RAW …` command. **Quoted phrases are supported**, not
merely tolerated.

### §S2 Correct search-syntax guidance (so callers build valid queries)
The `mail-search` guidance reflects the **actually-supported** compound grammar now that §S1 lands —
qualifiers, `OR`, parentheses, **and quoted phrases** — so the agent/user construct queries `voa` accepts:
- the `mail-search` verb's help/usage text states the supported query grammar with a quoted-phrase example;
- the skill's [`references/search-recipes.md`](../../skills/vidushi-oa/references/search-recipes.md) shows a
  compound example **including a quoted phrase** (removing any prior "avoid quotes" workaround wording).
This is the "correct hints for search" half — the fix supports quotes; the guidance advertises them.

## Acceptance criteria

Tests drive `GmailImapAdapter.search()` with a **fake IMAP connection** that captures the exact args passed
to `conn.uid(...)` — no live Gmail.

### §S1
- [ ] A query containing a quoted phrase (e.g. `category:purchases "out for delivery"`) yields a `conn.uid("SEARCH", "X-GM-RAW", <arg>)` call whose `<arg>` is a **valid IMAP quoted-string** — embedded `"` rendered as `\"` and `\` as `\\` (or the value sent as a literal) — asserted against the captured arg. It is **not** the naive `'"%s"' % query` output for a quoted-phrase input.
- [ ] **Regression:** a quote-free compound query (qualifiers + `OR` + parentheses, e.g. `category:purchases newer_than:3m`) produces a well-formed command equivalent to today's behaviour (no over-escaping).
- [ ] **Integration (production path):** the assertion exercises the public `GmailImapAdapter.search(query)` method end-to-end (fake conn), not a private quoting helper in isolation.

### §S2
- [ ] `voa mail-search --help` (or its usage/error text) states the supported compound grammar and includes a **quoted-phrase** example.
- [ ] `skills/vidushi-oa/references/search-recipes.md` contains a compound-query example **with a quoted phrase**, and contains no residual "avoid quoted phrases / quotes break Gmail" workaround wording.

## Estimated size
S — a quoting/escaping fix in one method + RED tests for the quoted-phrase and quote-free cases (§S1), plus a
small verb-help + skill search-recipes guidance update (§S2).

## Risk
Minimal — a string-quoting fix scoped to the Gmail adapter. The one care point is **not double-escaping**
already-safe queries (covered by the regression AC). No change to Yahoo/base search, credentials, or the
JMAP path.

## Non-goals
A full query-DSL / cross-provider query translator; changing Yahoo or base-adapter search; the separate
Fastmail JMAP `Content-Type` bug (CR-OA-024).
