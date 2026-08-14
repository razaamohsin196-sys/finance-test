# Copyright (c) 2026, Mohsin and contributors
# For license information, please see license.txt

import re

import frappe
from frappe import _
from frappe.model.document import Document


class VendorAccountRule(Document):
    def validate(self):
        try:
            re.compile(self.pattern)
        except re.error as exc:
            frappe.throw(_("Pattern is not a valid regular expression: {0}").format(exc))

        from ai_bookkeeper.core.accounts import ALLOWED_KEYS, is_valid

        if not is_valid(self.account_key):
            frappe.throw(
                _("Unknown account key {0}. Valid keys: {1}").format(
                    self.account_key, ", ".join(ALLOWED_KEYS)
                )
            )

        if self.force_review and not self.review_reason:
            frappe.throw(_("A rule that forces review must say why."))
