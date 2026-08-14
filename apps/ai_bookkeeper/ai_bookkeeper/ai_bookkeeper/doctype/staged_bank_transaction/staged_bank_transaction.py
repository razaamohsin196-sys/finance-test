# Copyright (c) 2026, Mohsin and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class StagedBankTransaction(Document):
    """A single imported bank line, before it becomes an accounting document.

    This DocType never touches the general ledger. It holds the proposal and
    the routing decision; posting happens only when a human submits the linked
    Journal Entry.
    """

    def validate(self):
        if not self.dedupe_hash:
            self.dedupe_hash = self.compute_dedupe_hash()

        if self.amount is not None and self.direction:
            # A negative amount is not rejected -- row 10 of the client's sample
            # is exactly that, and silently "fixing" it would destroy the signal
            # the reviewer needs. It is preserved and flagged downstream.
            pass

    def compute_dedupe_hash(self) -> str:
        from ai_bookkeeper.converters import to_raw_transaction

        return to_raw_transaction(self).dedupe_hash

    def on_trash(self):
        if self.workflow_state == "Posted":
            frappe.throw(_("Cannot delete a transaction that has already posted."))
