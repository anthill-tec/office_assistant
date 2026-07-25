# Calendar-reminder recipe

Create renewal / expiry / delivery reminders **on request** (never unprompted).

## Defaults
- **Calendar:** the user's **default** calendar id.
- **Timezone:** `Asia/Kolkata`.
- **All-day** events (not timed).
- **Tag the title** so events are findable later: `[sub-watch]` for subscription renewals,
  `[buy-watch]` for purchases / warranty expiries.
- **Lead time:** ~30 days before a warranty expiry or a registration / premium deadline; on or just
  before the charge date for a TOMBSTONE subscription ("cancel before `<date>`").
- **Recurrence → cadence:** match the event recurrence to the item's cadence (monthly / yearly)
  where one exists; a one-off deadline is a single all-day event.
- **Verify after writing:** re-read the created event and confirm the date / title / calendar landed.

## Harness caveat (Fastmail via FastmailMCP, Claude Code)
In a **headless terminal** (no user present to confirm a widget), use **`create_event`**, NOT
`compose_event`: `compose_event` stages the event in an interactive widget that needs the user to
confirm, so it never resolves in a non-interactive run. Use `compose_event` only when a user is
present to review and refine. *(This caveat is Fastmail / Claude-Code-specific; on another harness
use its own direct calendar-write call — the portable skill body stays harness-agnostic.)*
