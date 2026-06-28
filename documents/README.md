# Office Assistant — Document Store

Saved copies of **purchase documents** (purchase orders, invoices, receipts, credit notes) that the
`invoice-tracker` skill captures from mail. Proof-of-purchase backbone for warranty claims, returns/RMA,
support cases, and tax/expense records.

## Layout
```
documents/
  personal/   <vendor>/  <YYYY-MM-DD>_<vendor>_<doctype>_<number>.<ext>
  business/   <vendor>/  ...                                            # bought on @anthilllabs.in / GST
```
Filename convention: `2026-06-14_lioncircuits_invoice_INV-225862.pdf` (date first → sorts chronologically).

## Registry & lookup
The document **registry is JSONL**, not a markdown index: `../data/invoices.jsonl`, queried via
`../scripts/store.py` (each row carries the `file` path here + the `source` mail pin). lean-ctx indexes
this folder so files are discoverable. To find a document, query the store — don't scan the folder:
```bash
python3 scripts/store.py query invoices --where vendor=LionCircuits --fields number,date,amount,file
```

## Saving copies
Saving a copy means retrieving the attachment binary from the mailbox (Fastmail/Gmail) — done **with
per-file confirmation** (downloads need an OK). Until saved, the JSONL row still pins the source
(`email_id` + attachment name), so the document is always one step away even with no local copy.
