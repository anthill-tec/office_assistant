# Per-domain report templates

Shared conventions: tag every line `[FM]` / `[GM]` with ids / dates / amounts / AWB; **lead with
action-needed**; use the urgency ladder 🔴 (act now / deadline passing) · 🟠 (soon) · 🟢
(informational / on track).

## Subscription
**Act on** (🔴 failed payment, card expiring, TOMBSTONE renewal approaching) → **Upcoming renewals**
(🟠, with date + amount + KEEP/TOMBSTONE) → **Decide** (UNDECIDED dispositions) → **Receipts logged**.

## Purchase / delivery
**Not yet delivered** (open orders: merchant · stage · carrier · AWB · ETA, STUCK first) → **Customs /
action-needed** (🔴 duty / KYC / clarification, with AWB) → **Recently delivered** → **Missing invoice**
(delivered, no proof on file).

## Invoice / expense
**By document** (vendor · type · number · date · amount · GST · acct). Expense / tax view: **sum by
`acct` (personal / business) × period**, listing GST separately for business rows.

## Warranty
**Expiring soon** (🟠 within 30–60 days: product · expiry · invoice link) → **Registration due** →
**In force** → **Expired / renew-or-extend**.

## Insurance / regulatory
**Due now** (🔴 premium / registration / fitness inside the window: policy · asset · deadline) →
**Upcoming** → **Active**.

## Invoice retrieval — tier order
When a proof-of-purchase document is missing, retrieve in this order (cheapest first):
1. **Mail attachment** already in the inbox — search + save.
2. **Emailed link** to the vendor's invoice — fetch only if the domain is the vendor's official one.
3. **Login-gated portal** (Amazon.in, vendor portal): the **user logs in** via their own browser; the
   agent navigates / downloads — **never enter their credentials**.
4. **Ask the vendor** (a support draft) — last resort.
