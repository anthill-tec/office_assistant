# Subscription taxonomy & disposition rules

## Two-part category tag — `provider-kind / service-kind`
Tag each subscription / recurring item with a compact `provider-kind / service-kind` pair so
advice and reporting can group by type. Examples:
- `finance/bank`, `finance/broker`, `finance/upi-mandate`
- `security/password-manager`, `security/vpn`, `security/2fa`
- `media/streaming`, `media/music`, `media/news`
- `saas/dev-tools`, `saas/ai`, `saas/storage`, `saas/productivity`
- `utility/telecom`, `utility/electricity`, `utility/broadband`
- `commerce/membership` (Prime, etc.), `health/gym`, `health/pharmacy`

## Disposition — KEEP / TOMBSTONE / UNDECIDED (user-owned)
- **KEEP** → protect it: a failed payment or expiry is 🔴 urgent, a renewal is 🟢 expected.
- **TOMBSTONE** → flip the logic: an upcoming renewal becomes 🔴 "cancel before `<date>` so you're
  NOT charged".
- **UNDECIDED** → surface under a "Decide: keep or tombstone?" prompt and record the date the user
  decides. Never set disposition silently.

## Never-tombstone guard
**Never propose tombstoning** a `finance/bank` or `security/password-manager` item — cancelling
either can lock the user out of their money or their accounts. Surface those as KEEP-by-default and
flag any lapse as urgent.
