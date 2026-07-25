# Search recipes — per-domain mail queries (both mailboxes)

Search **both** mailboxes every pass, tag findings `[FM]` / `[GM]`, and merge/de-dupe by id.
Fastmail (`search_email`, FastmailMCP) takes Gmail-style qualifiers but **rejects parenthesized
`subject:(A OR B)` groups** — issue each phrase as its own query and merge the results yourself.
Gmail (`search_threads`, the claude.ai connector, `antojk@gmail.com`) takes the full standard
syntax — `OR`, parentheses, `category:`, `newer_than:`.

Common Gmail filters: `category:purchases` (best single filter for order/billing mail),
`category:updates`, `newer_than:3m`, `has:attachment`.

## Subscription / recurring billing
- **Fastmail** (folder `Subscriptions` + inbox; single phrases): `subject:receipt`,
  `subject:"payment failed"`, `subject:"your subscription"`, `subject:renewal`,
  `subject:"card expiring"`, `subject:"free trial"`.
- **Gmail:** `category:purchases (receipt OR subscription OR renewal OR "payment declined" OR "trial ending") newer_than:6m`.

## Purchase / delivery
- **Fastmail** (folders `Shipping`, `Purchases`): `subject:order`, `subject:shipped`,
  `subject:"out for delivery"`, `subject:delivered`, `subject:tracking`.
- **Gmail:** `category:purchases (order OR shipped OR "out for delivery" OR tracking OR delivered) newer_than:3m`.

## Customs / international
- **Fastmail:** `subject:customs`, `subject:duty`, `subject:KYC`, `subject:"India Post"`,
  `subject:clearance`, `subject:AWB`, `from:icegate`.
- **Gmail:** `(customs OR duty OR IGST OR KYC OR clearance OR "foreign post office") newer_than:6m`.

## Invoice / receipt (proof of purchase)
- **Fastmail:** `subject:invoice`, `subject:receipt`, `subject:"tax invoice"`, `subject:GST`,
  `has:attachment subject:invoice`.
- **Gmail:** `category:purchases (invoice OR receipt OR "tax invoice" OR GST) has:attachment newer_than:1y`.

## Warranty / registration
- **Fastmail:** `subject:warranty`, `subject:"register your product"`, `subject:"warranty card"`,
  `subject:AMC`.
- **Gmail:** `(warranty OR "register your product" OR "extended warranty" OR AMC) newer_than:1y`.

## Insurance / regulatory
- **Fastmail:** `subject:policy`, `subject:premium`, `subject:insurance`, `subject:RC`,
  `subject:fitness`.
- **Gmail:** `(policy OR premium OR insurance OR "registration certificate" OR fitness) newer_than:1y`.
