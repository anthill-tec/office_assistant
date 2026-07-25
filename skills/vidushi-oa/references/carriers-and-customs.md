# Carriers & customs reference

## Carrier roster (India-centric)
| Carrier | Scope | Tracking key | Notes |
|---|---|---|---|
| Delhivery | Domestic | AWB / waybill | Large e-commerce 3PL; SMS + email updates |
| DTDC | Domestic + intl | consignment no. | |
| Blue Dart | Domestic + intl (DHL group) | AWB | Premium/air; reliable ETAs |
| India Post (Speed Post / Regd) | Domestic + inbound intl | article / tracking id | Inbound international parcels clear via the **Foreign Post Office (FPO)** |
| Ekart | Domestic | tracking id | Flipkart logistics |
| Shadowfax | Domestic | tracking id | Quick-commerce / last-mile |
| FedEx | International | tracking no. | Broker handles customs; may email the recipient for KYC/duty |
| DHL | International | waybill | |
| UPS | International | tracking no. | |
| Aramex | International (ME / intl) | AWB | |

Match a delivery/customs mail to an order on the **AWB / tracking number**, not just the merchant
— a broker or the India Post FPO often emails the recipient directly with only the AWB.

## Customs (inbound international)
- Genuine customs / duty / IGST / KYC / clarification requests are **real and time-sensitive** — a
  missed one can get a parcel **returned or abandoned**. Never dismiss them.
- But fake "pay a customs fee" / "KYC" messages are the **top import scam**. Resolve by
  **verifying**, never guessing: the AWB matches a real expected shipment, and the sender is the
  **true carrier / India Post FPO / ICEGATE (CBIC)** domain.
- Pay or submit documents **only** via the carrier's official portal or the government
  **ICEGATE / CBIC** site using the AWB — never a link or button in the email.
- Customs sub-states are **OPEN actions on the order** (`customs-clearance`, `duty-payment`, `kyc`,
  `clarification`), so `voa attention` surfaces a parcel awaiting the user. A bare-AWB customs mail
  annotates the matching order or creates a minimal `orders` row (`status: IN_PROGRESS` + the open
  customs action).
