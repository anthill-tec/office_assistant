# CR-OA-024 — Fastmail JMAP POST missing `Content-Type` header (400s every request)

**Status:** PENDING
**Type:** bugfix
**Priority:** High
**Depends on:** 020
**Labels:** mail, jmap, fastmail, bug
**Phase:** Wave 10 (embedded mail send)
**Design reference:** [DN-mail-access.md](../research/DN-mail-access.md) §Decision 2 (transport hybrid — Fastmail JMAP)
**Author:** Antony John · **Co-author:** Vidushi (orchestrator — office-assistant)

## Context

`JmapAdapter._auth_headers()` (`vidushi_oa/mail/jmap.py:71–72`) returns only
`{"Authorization": f"Bearer {self.token}"}`. Fastmail's JMAP API endpoint **400s the POST**
(`_transport("POST", api_url, self._auth_headers(), body)`, `jmap.py:103`) when the request lacks
`Content-Type: application/json`. **Consequence:** `voa` cannot pull Fastmail at all — every JMAP call fails
— so Fastmail mail is **fully blocked** in `voa`. (Gmail is a separate IMAP path and is unaffected.) Shipped
in 1.0.0 (CR-OA-020); this fixes it in the 1.1.0 minor.

## Scope

### §S1 Send `Content-Type: application/json` on JMAP requests
`JmapAdapter._auth_headers()` returns a header map that includes **`"Content-Type": "application/json"`**
alongside the `Authorization` bearer. Both JMAP calls route through `_auth_headers()` — the session `GET`
(`jmap.py:78`) and the API `POST` (`jmap.py:103`) — so the header is present on the POST that Fastmail
rejects today (harmless on the bodyless GET).

## Acceptance criteria

### §S1
- [ ] `JmapAdapter._auth_headers()` returns a dict whose entries are exactly `Authorization: Bearer <token>` **and** `Content-Type: application/json` (assert both keys + values).
- [ ] **Integration (production POST path):** driving `JmapAdapter` through its public fetch/search path with the injected fake `transport` captures the headers passed to the `POST` at the JMAP `api_url`, and those headers include `Content-Type: application/json`. The test exercises the real method (not `_auth_headers()` in isolation).
- [ ] **Regression:** the existing JMAP session/query tests still pass with the added header (the GET path is unaffected).

## Estimated size
XS — a one-line header addition plus a RED test asserting the POST carries it.

## Risk
Minimal — adds a standard, expected request header. No credential, endpoint, or body change. The header is
inert on the bodyless session GET.

## Non-goals
Broader JMAP hardening (retry/backoff, error-body surfacing); any change to the IMAP adapters or the secret
resolver.
