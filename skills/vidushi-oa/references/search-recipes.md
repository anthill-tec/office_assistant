# Search recipes — per-domain `voa mail-search` queries

All mail read/fetch goes through **`voa mail-search '<query>'`** — it searches every configured
account (or the subset named with `--accounts a,b`), **merges + de-dupes by Message-ID**,
**source-tags each row `[FM]` Fastmail / `[GM]` Gmail / `[YH]` Yahoo**, and returns compact TOON.
You never issue raw per-provider MCP calls: the verb maps a **portable query** to each provider's
server-side search — Gmail `X-GM-RAW`, Fastmail JMAP filters, Yahoo/IMAP `SEARCH`.

**Portable qualifiers the verb accepts** (mapped per provider): `subject:`, `from:`, `to:`,
`newer_than:` — a count plus one of the units `d` / `w` / `m` / `y`, calendar-free (`w` = 7 days,
`m` = 30 days, `y` = 365 days), e.g. `30d`/`2w`/`3m`/`6m`/`1y` — `has:attachment`, `category:`
(`purchases`/`updates`/`promotions`), `OR` (implicit AND when no operator is given) / nestable
parenthesised groups, and quoted `"exact phrase"` matching (a quoted phrase is matched as a
contiguous phrase, e.g. `"out for delivery"`). **`category:` is Gmail-only** — JMAP (Fastmail) and
plain IMAP (Yahoo) accounts *refuse* it with a structured error rather than silently ignoring it,
and plain IMAP refuses `has:attachment` too; an unknown qualifier or unit, or unbalanced
parentheses, is likewise a loud error, never a silent empty result.
`category:purchases` is the best single filter for order/billing mail;
`has:attachment` narrows to document-bearing mail. Prefer one broad merged query over many single-phrase ones — the merge/tag
is done for you. Fetch a full hit with `voa mail-get --account <name> --uid <uid>`.

## Subscription / recurring billing
```
voa mail-search 'category:purchases (receipt OR subscription OR renewal OR "payment failed" OR "card expiring" OR "free trial") newer_than:6m'
```
Fastmail rows come from the `Subscriptions` folder + inbox and key on the per-merchant alias; Gmail
rows key on sender + `category:`.

## Purchase / delivery
```
voa mail-search 'category:purchases (order OR shipped OR "out for delivery" OR tracking OR delivered) newer_than:3m'
```
Fastmail `Shipping` / `Purchases` folders are covered by the same merged pass.

## Customs / international
```
voa mail-search '(customs OR duty OR IGST OR KYC OR clearance OR AWB OR "foreign post office" OR "India Post") newer_than:6m'
voa mail-search 'from:icegate newer_than:6m'
```

## Invoice / receipt (proof of purchase)
```
voa mail-search 'category:purchases (invoice OR receipt OR "tax invoice" OR GST) has:attachment newer_than:1y'
```

## Warranty / registration
```
voa mail-search '(warranty OR "register your product" OR "extended warranty" OR "warranty card" OR AMC) newer_than:1y'
```

## Insurance / regulatory
```
voa mail-search '(policy OR premium OR insurance OR "registration certificate" OR RC OR fitness) newer_than:1y'
```
