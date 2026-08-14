"""Translation between Frappe documents and the pure core dataclasses.

Kept in one small module on purpose: it is the only seam where ORM objects and
core value types meet, so there is exactly one place to look when a field name
changes.
"""

from __future__ import annotations

from decimal import Decimal

from frappe.utils import getdate

from ai_bookkeeper.core.models import Direction, RawTransaction


def to_raw_transaction(doc) -> RawTransaction:
    """Build a ``RawTransaction`` from a Staged Bank Transaction document."""
    return RawTransaction(
        txn_date=getdate(doc.txn_date),
        description=(doc.description or "").strip(),
        amount=Decimal(str(doc.amount)),
        currency=(doc.currency or "USD").upper(),
        direction=Direction(doc.direction),
        account_hint=(doc.account_hint or None),
    )
