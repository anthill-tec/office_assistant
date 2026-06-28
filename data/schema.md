# Office Assistant — Data Store Schema

All records are **JSONL** (one JSON object per line) under `data/`, accessed via
`scripts/store.py` (never hand-parse whole files — query through the script to save tokens).
Common fields: `id` (string, auto), `acct` (`personal`|`business`), `updated` (YYYY-MM-DD).
`source` pins the originating mail: `{"mailbox":"FM"|"GM","email_id":"...","thread_id":"...","attachment":"name.pdf"|null}`.

### Foreign keys & joins
Records reference each other by id. `store.py ... --expand <fk[,fk...]>` resolves them inline (adds
`<fk>_obj`), so one call does a rich query (e.g. product → its support contact → its invoice).
| FK field | → store | used on |
|---|---|---|
| `contact_id` | contacts | products (manufacturer support), warranties, cases, invoices |
| `invoice_id` | invoices | warranties, cases, products |
| `warranty_id` | warranties | cases, products, invoices |
| `product_id` | products | warranties, cases |
`contacts.kind` ∈ `reseller`|`manufacturer`|`service` distinguishes who a contact is. A product's
`contact_id` should point to a **manufacturer** (or service) contact, not the reseller it was bought from.
Example: `store.py get products prod_fnirsi_tmp-610s --expand contact_id,invoice_id,warranty_id`

## contacts — `vendor_contacts.jsonl`  (verified vendor support directory)
| field | type | notes |
|------|------|------|
| id | str | `ven_<vendor-slug>` |
| vendor | str | display name |
| kind | enum\|null | reseller \| manufacturer \| service |
| category | str | e.g. `electronics/pcb-fab` |
| acct | enum | personal \| business |
| support_email | str\|null | **verified** address only; null = `TBD` |
| portal | str\|null | support/returns URL |
| phone | str\|null | |
| account_ref | str\|null | customer/billing id |
| buying_alias | str\|null | the masked alias used to buy from them |
| rma_process | str\|null | how returns/claims are raised |
| verified_source | str | how the support contact was confirmed |
| notes | str\|null | |

## invoices — `invoices.jsonl`  (purchase documents: PO / invoice / receipt / credit note)
| field | type | notes |
|------|------|------|
| id | str | `doc_<vendor>_<number-or-date>` |
| doc_type | enum | po \| invoice \| receipt \| creditnote |
| vendor | str | |
| number | str\|null | PO/invoice/receipt number |
| date | str | YYYY-MM-DD |
| amount | num\|null | total |
| currency | str | e.g. INR, USD |
| tax_amount | num\|null | GST/VAT portion |
| gstin | str\|null | seller GSTIN (business) |
| acct | enum | personal \| business |
| order_ref | str\|null | links to the order |
| products | [str] | line items (short) |
| file | str\|null | saved copy path under `documents/...`, or null if only pinned |
| warranty_id | str\|null | link to a warranties record |
| source | obj | mail pin (see above) |

## warranties — `warranties.jsonl`
| field | type | notes |
|------|------|------|
| id | str | `war_<vendor>_<product-slug>` |
| product | str | |
| vendor | str | |
| model | str\|null | | serial | str\|null | |
| purchase_date | str | YYYY-MM-DD |
| term_months | int\|null | coverage length |
| expiry | str | computed YYYY-MM-DD |
| extended | bool | extended warranty/AMC present |
| registration | obj | `{"required":bool,"deadline":str|null,"done":bool}` |
| acct | enum | personal \| business |
| invoice_id | str\|null | proof-of-purchase link |
| source | obj | mail pin |

## products — `product_catalogue.jsonl`  (owned-product knowledge base — keyed on the MANUFACTURER)
Focus is the **actual manufacturer** (the real maker), NOT the reseller/marketplace it was bought through.
Links must point to the **manufacturer's official** resources, never an intermediary's listing.
| field | type | notes |
|------|------|------|
| id | str | `prod_<manufacturer-slug>_<model-or-product-slug>` |
| product | str | what it is |
| manufacturer | str | **the maker** — the catalogue key |
| bought_from | str\|null | reseller/marketplace/intermediary (links to the invoice's `vendor`) |
| model | str\|null | | serial | str\|null | (usually on the warranty record) |
| category | str\|null | |
| links | obj | manufacturer-official only: `{product_page, manual, datasheet, support, drivers_firmware, spec_sheet, community}` (each url or null) |
| key_specs | str\|null | short spec summary |
| invoice_id | str\|null | proof-of-purchase link | warranty_id | str\|null | coverage link |
| contact_id | str\|null | FK → contacts (manufacturer support/service) |
| acct | enum | personal \| business |
| links_verified | str\|null | how/when the manufacturer links were confirmed (official domain) |
| source | obj | mail pin where the product was identified |
| notes | str\|null | |

## cases — `support_cases.jsonl`  (support / claim / RMA / service)
| field | type | notes |
|------|------|------|
| id | str | `case_<vendor>_<n>` |
| vendor | str | | product | str\|null | |
| warranty_id | str\|null | | invoice_id | str\|null | |
| issue | str | short summary |
| channel | enum | email \| portal \| phone |
| ticket | str\|null | vendor ticket/RMA number |
| status | enum | open \| awaiting_support \| awaiting_user \| rma_issued \| in_repair \| resolved \| closed |
| opened | str | YYYY-MM-DD |
| last_contact | obj | `{"date":str,"by":"support"|"user"}` |
| next_action | str\|null | | owner | enum | user \| support |
| due | str\|null | next-action due date |
| deadline | str\|null | SLA / warranty-window deadline |
| threads | [obj] | `[{"mailbox":"FM"|"GM","thread_id":"..."}]` |
| log | [obj] | `[{"date":str,"note":str}]` append-only history |
| acct | enum | personal \| business |
