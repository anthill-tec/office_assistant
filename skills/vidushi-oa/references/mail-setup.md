# Mail setup — agent-guided, secret-free onboarding

This guide is how the skill **onboards a mailbox** into `voa`'s embedded mail client. The skill
**orchestrates guidance + verification only** — it walks the user through generating a provider
credential, then hands off to the interactive `voa mail-auth` prompt. **The agent never sees or
handles the secret** — it flows user→`voa` directly, typed at `voa`'s hidden prompt; the agent
only ever runs the non-secret verbs (`voa mail-accounts`, `voa doctor`).

Each account carries a source tag: `[FM]` Fastmail, `[GM]` Gmail, `[YH]` Yahoo.

## Step 1 — generate the credential (per provider)

- **Fastmail `[FM]`** — a **JMAP API token**. In Fastmail: **Settings → Privacy &
  Security → API tokens → New token**, and copy the **token**. (JMAP is Fastmail's native API; the
  token is what `voa mail-search` uses for JMAP filter queries.) Scope it **read-only** *unless* you
  opt this account into sending (Step 2) — the send path uploads a blob, `Email/import`s a draft and
  submits it, so a send-capable account needs a **write + submission** token; a read-only one leaves
  reads working while every draft/send fails.
- **Gmail personal `[GM]`** — an **IMAP app password** (an "app-password"). Requires **2FA
  (2-Step Verification) enabled**: Google Account → Security → 2-Step Verification → **App
  passwords** → generate one for "Mail". Copy the 16-character **app password**.
- **Yahoo `[YH]`** — an **IMAP app password**, same idea. Requires 2FA: Yahoo Account Security →
  **Generate app password** → copy it.
- **Gmail Workspace (app passwords disabled by admin)** — use the **XOAUTH2** path instead: create
  an **OAuth client** in Google Cloud, authorise the Gmail scope, and obtain a **refresh token**.
  `voa mail-auth` stores the OAuth client id/secret + refresh token; the XOAUTH2 access token is
  minted at query time. Use this only when app passwords are unavailable. If the account is also
  opted into sending (`--send`), authorise the **full** `https://mail.google.com/` scope — the same
  access token authenticates SMTP submission, and a read-only scope leaves reads working while every
  send fails at `AUTH`.

## Step 2 — hand the secret to `voa` (agent never touches it)

Run the **interactive** auth verb and let the **user type the secret at `voa`'s hidden prompt**:

```bash
voa mail-auth --provider fastmail --address you@fastmail.com     # [FM] read-only JMAP token
voa mail-auth --provider gmail    --address you@gmail.com        # [GM] IMAP app password (or XOAUTH2)
voa mail-auth --provider yahoo    --address you@yahoo.com        # [YH] IMAP app password
```

`voa` prompts for the secret with input hidden; **the agent never sees or handles the secret**. For
XOAUTH2, `voa mail-auth --provider gmail --address you@workspace.com` prompts for the OAuth client
details + refresh token the same hidden way. An optional `--secret-ref <ref>` points `voa` at a
secret already in a keyring/secret store instead of prompting.

**Sending is opt-in per account.** Accounts are **read-only by default** — `voa mail-send` refuses an
account that was not registered with **`--send`**, so grant it only where the user wants outbound mail,
and add every extra From identity (a Fastmail masked alias, …) with a repeatable **`--alias`** (the
From-identity guard accepts the account address plus every configured alias):

```bash
voa mail-auth --provider fastmail --address you@fastmail.com --send --alias vendor.alias@fastmail.com
```

**Non-interactive / CI escape:** pipe the secret to `voa mail-auth` on **stdin** (e.g.
`voa mail-auth --provider yahoo --address you@yahoo.com < secret.txt`) — still never pasted into the
conversation.

## Step 3 — verify

```bash
voa mail-accounts     # confirms the account is now configured (tagged [FM]/[GM]/[YH])
voa doctor            # diagnoses per-account connectivity (auth OK, server reachable)
```

If `voa doctor` reports an auth failure, re-run `voa mail-auth` for that account with a freshly
generated credential — a revoked/expired app password or token is the usual cause.
