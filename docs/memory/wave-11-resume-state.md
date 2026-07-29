# Wave 11 (1.1.2) — resume state

**Saved:** 2026-07-30 (session ended mid-cycle during a Claude outage) · **Nothing uncommitted, everything pushed to origin.**

## Where Wave 11 came from

Two **field bug reports** arrived over Sandesh against the installed **1.1.1** (from `Mainline - vidushi_oa`,
the workspace *using* the release): msgs **1346** (Fastmail JMAP search dead + `orders.order_date` +
packaging) and **1347** (`newer_than:1w` silent 0, `mail-get` envelope-only). They were **triaged against
the code and then empirically validated** by a new read-path E2E against the Stalwart JMAP emulator —
which **overturned two of the reported diagnoses** and found the real root cause. See
[`docs/changes/README.md`](../changes/README.md) footer note "Wave 11 → 1.1.2" and the four CR specs.

## CR status

| CR | State |
|---|---|
| **CR-OA-030** JMAP read-path correctness | ✅ **COMPLETED**, merged to `develop` |
| **CR-OA-031** portable query translation | ✅ **COMPLETED**, merged to `develop` |
| **CR-OA-032** `mail-get` full message | 🔵 **IN PROGRESS** — see resume point below |
| **CR-OA-033** validator-drift detection | ⬜ not started |

## Exact resume point

- **Branch:** `feature/CR-OA-032-mail-get-full-message` (pushed; head `e193cce`).
- **`develop`** = `8c7f5d6` = origin. `main`/tag `1.1.1` already released and live on PyPI.
- **Cycle plan for CR-032:** C1 §S1 body · C2 §S2 `to`/`cc` + RFC 2047 · C3 §S3/§S4 AXI
  (truncation/`--full`/`next[]`, search rows stay body-free) · C4 VERIFY (+FIX) · C5 gate + close.
- **NEXT ACTION: GREEN for C1.** RED is done and committed (`e193cce`, `tests/test_cr_oa_032_mail_get_body.py`,
  **12 failing tests**). The RED author pinned the contract in that file's module docstring:
  - `cmd_mail_get` payload gains **`body`** (decoded `text/plain`, else `text/html` stripped to text) and
    **`attachments`** (`[{"filename", "size"}]` — decoded size, never bytes, never a JMAP `blobId` payload).
  - **IMAP** (`mail/imap.py` `fetch_message`/`_fetch_spec`): currently fetches only
    `BODY.PEEK[HEADER.FIELDS …]`. Must fetch + decode the body (honour `Content-Transfer-Encoding`
    base64/quoted-printable and the charset, UTF-8-with-replacement fallback, never raise). **Reuse/extend
    `fetch_html_body`** — do not build a parallel MIME path. Stdlib `email` only.
  - **JMAP** (`mail/jmap.py` `fetch_message`): add `textBody`/`htmlBody`/`bodyValues` + attachment metadata,
    following `fetch_html_body`'s `bodyValues[partId]["value"]` pattern. JMAP `bodyValues` arrive
    **server-decoded** (RFC 8621), so there is no transfer-encoding to undo there.
  - **Folded in from CR-030's VERIFY:** route `jmap._email_get_list`/`fetch_message` through CR-030's shared
    `_raise_for_method_error` so a method-level `Email/get` error surfaces the server's real
    `type`/`description` instead of `"message not found"`; and drop the stale `cmd_mail_get` docstring claim
    that `JmapAdapter` raises `NotImplementedError`.
  - Scope C1 to §S1 only (no `to`/`cc`, no RFC 2047, no AXI truncation — those are C2/C3). Stdlib only.

## Process rules in force (learned the hard way this wave)

- **no-mistakes is the FINAL RELEASE GATE, run ONCE before the release — never per cycle.** Correctness is
  established inside each CR's RED → GREEN → VERIFY cycle.
- **VERIFY owns the AC checkboxes**; RED/GREEN/FIX agents must never tick them.
- **Close a CR by touching BOTH** the queue row and the spec's `**Status:**` in the same merge diff.
- Agents must **escalate contradictions rather than "fix" failing tests** — that discipline is what caught
  the `deliveredTo` array shape, the advertised-but-unimplemented parenthesised groups, and the JMAP
  `category:` silent drop.
- Run `make e2e` (local-only tier, Docker) before any release and after any mail change.

## Open items needing the user

1. **Yank 1.1.0 on PyPI** (still outstanding; agent will not touch PyPI credentials).
2. **Capability narrowing — decision wanted.** CR-031's strict grammar makes Gmail-native operators outside
   the portable set (`rfc822msgid:`, `label:`, `larger:`, `filename:` …) a **loud parse error**; they
   previously worked via raw passthrough on Gmail (and silently did nothing on Fastmail). Three test queries
   had to be re-vehicled because of it. Possible follow-up: a documented **raw-passthrough escape hatch**
   (e.g. `mail-search --raw`, refused on providers that cannot take it) or case-by-case grammar extension.
   Deliberately **not** scoped into 1.1.2.
3. **Sandesh:** registered as `Mainline - office_assistant`; reports 1346/1347 were ACKed by **msg 1348**
   (FYI, in-progress). A **completion `reply` to msg 1346 is owed once 1.1.2 publishes**, so the reporter can
   re-verify their Bugs 1/2/3/5/6/7 in one pass. Relaunch the listener with
   `sandesh notify --project office_assistant --to "Mainline - office_assistant"`.

## After CR-032 + CR-033 — the 1.1.2 release run

Follow the mandatory checklist in [`../../AGENTS.md`](../../AGENTS.md) "Release process": no-mistakes on the
release branch **then reconcile onto the gate ref** → TestPyPI dry-run pinned to the release branch → release
gate (`env -u VIDUSHI_BACKEND …`) → full suite (live `mongod`) → **`make e2e`** → explicit
irreversible-publish confirm → `git flow release finish 1.1.2` + the single `git push origin main --tags` →
monitor CI → push the `develop` back-merge + delete the release branch.
