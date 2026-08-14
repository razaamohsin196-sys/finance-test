# Copyright (c) 2026, Mohsin and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class AICategorizationLog(Document):
    """Append-only audit record.

    No role has write or delete permission on this DocType (see the JSON), so
    the only writer is server code running with ignore_permissions. These hooks
    close the remaining gap: even a privileged path cannot quietly rewrite the
    AI's original proposal after the fact.

    The fields a human legitimately fills in later -- their decision, the
    account that actually posted, the timestamp -- are whitelisted for update.
    Everything describing what the model proposed is frozen once written.
    """

    #: Fields the approval step is allowed to fill in after creation.
    MUTABLE_AFTER_CREATION = {
        "human_decision",
        "final_account",
        "decided_by",
        "decided_at",
        "was_overridden",
        "journal_entry",
    }

    def on_update(self):
        before = self.get_doc_before_save()
        if not before:
            return

        frozen = [
            field
            for field in ("source", "proposed_account", "proposed_account_key",
                          "confidence", "tier", "model", "prompt_version",
                          "rationale", "review_reason", "raw_response", "transaction",
                          "shadow_account", "shadow_account_key", "shadow_confidence",
                          "shadow_rationale", "model_disagreed")
            if before.get(field) != self.get(field)
        ]
        if frozen:
            frappe.throw(
                _("An AI Categorization Log is append-only. Cannot change: {0}").format(
                    ", ".join(frozen)
                )
            )

    def on_trash(self):
        frappe.throw(_("AI Categorization Log records cannot be deleted."))
