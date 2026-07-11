# Office Assistant — Reference (moved to the data store)

Reference/lookup data now lives in **MongoDB** (accessed via `../scripts/store.py`, token-frugal
queries; snapshotted to `../data/*.jsonl` with the schema in `../data/schema.md`) rather than markdown
lists here.

- **Vendor support contacts** → `../data/vendor_contacts.jsonl` (`store.py ... contacts`)
- **Schema** → `../data/schema.md` · **Script usage** → `../scripts/README.md`

```bash
python3 ../scripts/store.py query contacts --where vendor=LionCircuits --fields support_email,portal,rma_process
```

**Safety hook:** `support-case-manager` may draft outbound mail to support. It must address replies only
to a **verified** address from `vendor_contacts.jsonl` or one the user supplies in chat — never an address
scraped from an unverified email. Sending is always draft-then-confirm.
