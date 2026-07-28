"""CR-OA-028 §S3 — entity -> store candidate mapping (RED).

`vidushi_oa/mail/extract_map.py` does not exist yet, so every test below fails
today with a `ModuleNotFoundError` raised from `setUp` (imported lazily via
`importlib`, mirroring the §S2 RED test's pattern, so pytest reports one
meaningful per-test failure each rather than a single file-level collection
crash).

Pinned shape for GREEN (per the CR-OA-028 §S3 scope + dispatch design):

  - New module `vidushi_oa/mail/extract_map.py`.
  - `to_store_candidates(entities: list[dict]) -> list[dict]` maps the §S2
    `extract_schema_org` output (or hand-built equivalents) onto candidate
    rows shaped `{"type": "orders"|"invoices", "candidate": {<store fields>}}`
    — the store fields are the REAL columns from `data/schema.md` /
    `vidushi_oa/schema/orders.schema.json` / `invoices.schema.json`, never
    invented ones.

  Field sourcing pinned by this RED pass:

  * `Order` -> an `orders` candidate:
      - `number`      <- `orderNumber`                (omitted if absent)
      - `merchant`    <- `seller.name`                 (omitted if absent)
      - `items`       <- `orderedItem[].orderedItem.name` (or `.name` directly),
                         a compact list of item names   (omitted if absent)
      - `order_date`  <- `orderDate`                   (omitted if absent)
      - `eta`         <- `orderDelivery.expectedArrivalUntil` (omitted if absent)
      - `status`/`stage` <- `orderStatus` through the STATUS_MAP below, ALWAYS
                         present (default `("NEW", None)` when the status is
                         absent or not in the map).
    When the Order carries invoice-ish proof (`partOfInvoice`), ALSO emits an
    `invoices` proof candidate (see below), appended AFTER the `orders`
    candidate. The proof candidate's `date` is sourced from the Order's own
    `orderDate` (a `partOfInvoice` sub-object rarely carries its own date).

  * `Invoice` -> an `invoices` candidate:
      - `number`   <- `confirmationNumber`
      - `amount`   <- `totalPaymentDue.value`
      - `currency` <- `totalPaymentDue.currency`
      - `date`     <- `paymentDueDate` (schema.org's `Invoice` has no distinct
                       issue-date property; this is a documented semantic
                       approximation, flagged for GREEN/reviewer attention)

  * `ParcelDelivery` -> `orders` delivery fields:
      - `tracking` <- `trackingNumber`                 (omitted if absent)
      - `carrier`  <- `carrier.name`                   (omitted if absent)
      - `eta`      <- `expectedArrivalUntil`            (omitted if absent)
      - `status`/`stage` <- `deliveryStatus` through the SAME STATUS_MAP
                       (delivery status and order status share the store's
                       status/stage vocabulary; same default rule)
      - `deliveryAddress` has **no matching `orders` store field**
        (`data/schema.md`'s `orders` field table carries no address column) —
        GREEN must OMIT it, never invent a new store field for it.

  STATUS_MAP (schema.org token, tolerant of a full `https://schema.org/...`
  IRI or the bare token -> `(store status, store stage)`), pinned to the REAL
  `orders` vocabulary (`vidushi_oa/schema/orders.schema.json`'s 4-value
  `status` enum + `data/schema.md`'s `stage` free-text vocabulary):

      "OrderProcessing" -> ("IN_PROGRESS", "Processing")
      "OrderShipped"    -> ("IN_PROGRESS", "Shipped")
      "OrderInTransit"  -> ("IN_PROGRESS", "In transit")
      "OrderDelivered"  -> ("COMPLETED",   "Delivered")
      "OrderCancelled"  -> ("COMPLETED",   "Cancelled")
      "OrderReturned"   -> ("COMPLETED",   "Returned")
      <absent or anything else, e.g. "OrderProblem"> -> ("NEW", None)

No real mail/network anywhere in this file — every sample is a small,
artificial, hand-built entity dict with fictitious order/invoice numbers and
addresses (the no-personal-data invariant), matching the shape
`extract_schema_org` (§S2) would hand back.
"""
import importlib
import unittest


def _import_extract_map():
    return importlib.import_module("vidushi_oa.mail.extract_map")


class OrderToOrdersCandidateTest(unittest.TestCase):
    """§S3 AC: an `Order` entity yields an `orders` candidate with the order
    number, >=1 line item, and `orderStatus` mapped to the store's real
    status/stage vocabulary (exact fields asserted)."""

    def setUp(self):
        self.extract_map = _import_extract_map()

    def test_full_order_entity_maps_to_orders_candidate(self):
        order_entity = {
            "@type": "Order",
            "orderNumber": "ORD-1001",
            "seller": {"@type": "Organization", "name": "Acme Traders"},
            "orderedItem": [
                {"@type": "OrderItem", "orderedItem": {"@type": "Product", "name": "Widget A"}},
                {"@type": "OrderItem", "orderedItem": {"@type": "Product", "name": "Widget B"}},
            ],
            "orderDate": "2026-07-01",
            "orderDelivery": {"@type": "ParcelDelivery", "expectedArrivalUntil": "2026-07-10"},
            "orderStatus": "https://schema.org/OrderDelivered",
        }

        result = self.extract_map.to_store_candidates([order_entity])

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0], {
            "type": "orders",
            "candidate": {
                "number": "ORD-1001",
                "merchant": "Acme Traders",
                "items": ["Widget A", "Widget B"],
                "order_date": "2026-07-01",
                "eta": "2026-07-10",
                "status": "COMPLETED",
                "stage": "Delivered",
            },
        })


class ParcelDeliveryToOrdersCandidateTest(unittest.TestCase):
    """§S3 AC: a `ParcelDelivery` entity yields `orders` delivery fields
    carrying `tracking` == trackingNumber and `carrier` == the carrier's
    name; the delivery address has no store field and must be omitted."""

    def setUp(self):
        self.extract_map = _import_extract_map()

    def test_parcel_delivery_entity_maps_tracking_and_carrier(self):
        delivery_entity = {
            "@type": "ParcelDelivery",
            "trackingNumber": "AWB123456789",
            "carrier": {"@type": "Organization", "name": "BlueDart"},
            "deliveryAddress": {"@type": "PostalAddress", "streetAddress": "221B Baker Street"},
            "expectedArrivalUntil": "2026-08-02",
            "deliveryStatus": "OrderInTransit",
        }

        result = self.extract_map.to_store_candidates([delivery_entity])

        self.assertEqual(len(result), 1)
        candidate = result[0]
        self.assertEqual(candidate["type"], "orders")
        self.assertEqual(candidate["candidate"]["tracking"], "AWB123456789")
        self.assertEqual(candidate["candidate"]["carrier"], "BlueDart")
        # Exact whole-dict equality: guards against a naive mapper leaking
        # `deliveryAddress` (or any raw entity field) straight into the
        # candidate instead of omitting fields with no store counterpart.
        self.assertEqual(candidate, {
            "type": "orders",
            "candidate": {
                "tracking": "AWB123456789",
                "carrier": "BlueDart",
                "eta": "2026-08-02",
                "status": "IN_PROGRESS",
                "stage": "In transit",
            },
        })
        self.assertNotIn("deliveryAddress", candidate["candidate"])
        self.assertNotIn("streetAddress", str(candidate))


class InvoiceToInvoicesCandidateTest(unittest.TestCase):
    """§S3 AC: a standalone `Invoice` entity yields an `invoices` candidate
    with number, total (amount/currency), and date."""

    def setUp(self):
        self.extract_map = _import_extract_map()

    def test_invoice_entity_maps_number_amount_currency_date(self):
        invoice_entity = {
            "@type": "Invoice",
            "confirmationNumber": "INV-5001",
            "totalPaymentDue": {"@type": "MonetaryAmount", "value": 2499.00, "currency": "INR"},
            "paymentDueDate": "2026-07-15",
        }

        result = self.extract_map.to_store_candidates([invoice_entity])

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0], {
            "type": "invoices",
            "candidate": {
                "number": "INV-5001",
                "amount": 2499.00,
                "currency": "INR",
                "date": "2026-07-15",
            },
        })


class OrderWithInvoiceProofYieldsBothCandidatesTest(unittest.TestCase):
    """§S3 AC: an `Order` carrying invoice-ish proof (`partOfInvoice`) yields
    BOTH an `orders` candidate AND an `invoices` proof candidate, in that
    order — never just one or the other."""

    def setUp(self):
        self.extract_map = _import_extract_map()

    def test_order_with_part_of_invoice_yields_orders_and_invoices_candidates(self):
        order_entity = {
            "@type": "Order",
            "orderNumber": "ORD-2002",
            "seller": {"@type": "Organization", "name": "Bright Retail"},
            "orderedItem": [
                {"@type": "OrderItem", "orderedItem": {"@type": "Product", "name": "Gadget X"}},
            ],
            "orderDate": "2026-06-20",
            "orderStatus": "OrderProcessing",
            "partOfInvoice": {
                "@type": "Invoice",
                "confirmationNumber": "INV-7007",
                "totalPaymentDue": {"@type": "MonetaryAmount", "value": 899.50, "currency": "INR"},
            },
        }

        result = self.extract_map.to_store_candidates([order_entity])

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0], {
            "type": "orders",
            "candidate": {
                "number": "ORD-2002",
                "merchant": "Bright Retail",
                "items": ["Gadget X"],
                "order_date": "2026-06-20",
                "status": "IN_PROGRESS",
                "stage": "Processing",
            },
        })
        self.assertEqual(result[1], {
            "type": "invoices",
            "candidate": {
                "number": "INV-7007",
                "amount": 899.50,
                "currency": "INR",
                "date": "2026-06-20",
            },
        })

    def test_order_without_part_of_invoice_yields_only_orders_candidate(self):
        # Negative/bound companion: an Order with NO invoice-ish proof must
        # NOT spuriously emit a second (invented) invoices candidate.
        order_entity = {
            "@type": "Order",
            "orderNumber": "ORD-3003",
            "orderStatus": "OrderShipped",
        }

        result = self.extract_map.to_store_candidates([order_entity])

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["type"], "orders")


class OrderStatusVocabMappingTest(unittest.TestCase):
    """§S3 AC: table-driven — several schema.org order/delivery statuses map
    to the exact store status/stage pair; an unknown or absent status maps
    to the store's NEW/initial default rather than raising or inventing a
    value outside the real 4-value status enum."""

    def setUp(self):
        self.extract_map = _import_extract_map()

    def _mapped_status_stage(self, order_status):
        entity = {"@type": "Order", "orderNumber": "ORD-STATUS-TEST"}
        if order_status is not None:
            entity["orderStatus"] = order_status
        result = self.extract_map.to_store_candidates([entity])
        candidate = result[0]["candidate"]
        return candidate["status"], candidate["stage"]

    def test_order_processing_maps_to_in_progress_processing(self):
        self.assertEqual(self._mapped_status_stage("OrderProcessing"), ("IN_PROGRESS", "Processing"))

    def test_order_shipped_maps_to_in_progress_shipped(self):
        self.assertEqual(self._mapped_status_stage("OrderShipped"), ("IN_PROGRESS", "Shipped"))

    def test_order_in_transit_maps_to_in_progress_in_transit(self):
        self.assertEqual(self._mapped_status_stage("OrderInTransit"), ("IN_PROGRESS", "In transit"))

    def test_order_delivered_maps_to_completed_delivered(self):
        self.assertEqual(self._mapped_status_stage("OrderDelivered"), ("COMPLETED", "Delivered"))

    def test_order_cancelled_maps_to_completed_cancelled(self):
        self.assertEqual(self._mapped_status_stage("OrderCancelled"), ("COMPLETED", "Cancelled"))

    def test_order_returned_maps_to_completed_returned(self):
        self.assertEqual(self._mapped_status_stage("OrderReturned"), ("COMPLETED", "Returned"))

    def test_full_iri_status_normalises_same_as_bare_token(self):
        # Same tolerant normalisation §S2's microdata parser already applies
        # to `@type`/`itemtype` (last path segment of a URL).
        self.assertEqual(
            self._mapped_status_stage("https://schema.org/OrderDelivered"),
            ("COMPLETED", "Delivered"),
        )

    def test_unrecognised_status_defaults_to_new_with_no_stage(self):
        # A real schema.org OrderStatus enum member ("OrderProblem") that is
        # NOT in our pinned subset must still default safely, never raise.
        self.assertEqual(self._mapped_status_stage("OrderProblem"), ("NEW", None))

    def test_absent_status_defaults_to_new_with_no_stage(self):
        self.assertEqual(self._mapped_status_stage(None), ("NEW", None))


class NoEntitiesYieldsNoCandidatesTest(unittest.TestCase):
    """§S3 AC: `to_store_candidates([])` returns `[]` — the definitive empty
    state, not None, not an error."""

    def setUp(self):
        self.extract_map = _import_extract_map()

    def test_empty_entity_list_returns_empty_candidate_list(self):
        self.assertEqual(self.extract_map.to_store_candidates([]), [])
