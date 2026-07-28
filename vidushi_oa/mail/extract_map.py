"""Map schema.org entities onto store candidate rows (CR-OA-028 §S3).

Consumes the :func:`vidushi_oa.mail.schema_org.extract_schema_org` output (or
hand-built equivalents) and produces candidate rows shaped
``{"type": "orders"|"invoices", "candidate": {<store fields>}}`` — every
candidate field is a REAL column from ``data/schema.md`` /
``vidushi_oa/schema/orders.schema.json`` / ``vidushi_oa/schema/invoices.schema.json``.
Fields whose source is absent are OMITTED; entity fields with no store
counterpart (e.g. a ParcelDelivery ``deliveryAddress``) are dropped rather
than invented.

Pure function: no I/O, no network, stdlib only.
"""
from typing import Optional

# schema.org OrderStatus / delivery status token -> (store status, store stage).
# The store `status` values come from `orders.schema.json`'s 4-value enum; the
# `stage` values from `data/schema.md`'s free-text stage vocabulary.
STATUS_MAP: dict[str, tuple[str, str]] = {
    "OrderProcessing": ("IN_PROGRESS", "Processing"),
    "OrderShipped": ("IN_PROGRESS", "Shipped"),
    "OrderInTransit": ("IN_PROGRESS", "In transit"),
    "OrderDelivered": ("COMPLETED", "Delivered"),
    "OrderCancelled": ("COMPLETED", "Cancelled"),
    "OrderReturned": ("COMPLETED", "Returned"),
}

_DEFAULT_STATUS_STAGE: tuple[str, Optional[str]] = ("NEW", None)


def _normalise_status_token(value) -> Optional[str]:
    """Reduce a bare token OR a full ``https://schema.org/...`` IRI to its last
    path segment, mirroring the §S2 microdata parser's ``@type`` handling."""
    if not isinstance(value, str) or not value:
        return None
    return value.rstrip("/").split("/")[-1]


def _map_status_stage(raw_status) -> tuple[str, Optional[str]]:
    token = _normalise_status_token(raw_status)
    if token is None:
        return _DEFAULT_STATUS_STAGE
    return STATUS_MAP.get(token, _DEFAULT_STATUS_STAGE)


def _order_item_names(ordered_item) -> list[str]:
    """Compact list of item names from an ``orderedItem`` list.

    Each element is either an ``OrderItem`` wrapping the product under its own
    nested ``orderedItem`` key, or a product-like dict carrying ``name``
    directly.
    """
    names: list[str] = []
    if not isinstance(ordered_item, list):
        return names
    for element in ordered_item:
        if not isinstance(element, dict):
            continue
        nested = element.get("orderedItem")
        if isinstance(nested, dict) and nested.get("name"):
            names.append(nested["name"])
        elif element.get("name"):
            names.append(element["name"])
    return names


def _invoice_candidate(invoice: dict, date: Optional[str]) -> dict:
    """Build an ``invoices`` candidate from an Invoice-ish sub-object."""
    candidate: dict = {}
    number = invoice.get("confirmationNumber")
    if number is not None:
        candidate["number"] = number
    total = invoice.get("totalPaymentDue")
    if isinstance(total, dict):
        if total.get("value") is not None:
            candidate["amount"] = total["value"]
        if total.get("currency") is not None:
            candidate["currency"] = total["currency"]
    if date is not None:
        candidate["date"] = date
    return candidate


def _map_order(entity: dict) -> list[dict]:
    candidate: dict = {}
    if entity.get("orderNumber") is not None:
        candidate["number"] = entity["orderNumber"]
    seller = entity.get("seller")
    if isinstance(seller, dict) and seller.get("name") is not None:
        candidate["merchant"] = seller["name"]
    items = _order_item_names(entity.get("orderedItem"))
    if items:
        candidate["items"] = items
    if entity.get("orderDate") is not None:
        candidate["order_date"] = entity["orderDate"]
    delivery = entity.get("orderDelivery")
    if isinstance(delivery, dict) and delivery.get("expectedArrivalUntil") is not None:
        candidate["eta"] = delivery["expectedArrivalUntil"]
    status, stage = _map_status_stage(entity.get("orderStatus"))
    candidate["status"] = status
    candidate["stage"] = stage

    results = [{"type": "orders", "candidate": candidate}]

    invoice = entity.get("partOfInvoice")
    if isinstance(invoice, dict):
        proof = _invoice_candidate(invoice, entity.get("orderDate"))
        results.append({"type": "invoices", "candidate": proof})
    return results


def _map_invoice(entity: dict) -> list[dict]:
    # schema.org's Invoice has no distinct issue-date property; approximate the
    # store's `date` from `paymentDueDate` (documented semantic approximation).
    candidate = _invoice_candidate(entity, entity.get("paymentDueDate"))
    return [{"type": "invoices", "candidate": candidate}]


def _map_parcel_delivery(entity: dict) -> list[dict]:
    candidate: dict = {}
    if entity.get("trackingNumber") is not None:
        candidate["tracking"] = entity["trackingNumber"]
    carrier = entity.get("carrier")
    if isinstance(carrier, dict) and carrier.get("name") is not None:
        candidate["carrier"] = carrier["name"]
    if entity.get("expectedArrivalUntil") is not None:
        candidate["eta"] = entity["expectedArrivalUntil"]
    status, stage = _map_status_stage(entity.get("deliveryStatus"))
    candidate["status"] = status
    candidate["stage"] = stage
    # `deliveryAddress` has no matching `orders` store field: omitted.
    return [{"type": "orders", "candidate": candidate}]


def to_store_candidates(entities: list[dict]) -> list[dict]:
    """Map schema.org entities onto store candidate rows.

    Returns a list of ``{"type": "orders"|"invoices", "candidate": {...}}``
    rows — ``[]`` for an empty (or entirely unrecognised) input.
    """
    candidates: list[dict] = []
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        entity_type = entity.get("@type")
        if entity_type == "Order":
            candidates.extend(_map_order(entity))
        elif entity_type == "Invoice":
            candidates.extend(_map_invoice(entity))
        elif entity_type == "ParcelDelivery":
            candidates.extend(_map_parcel_delivery(entity))
    return candidates
