# Copyright (c) 2026, Mohsin and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class AIBookkeepingSettings(Document):
    def validate(self):
        if not 0 < float(self.confidence_threshold or 0) <= 1:
            frappe.throw(_("Confidence Threshold must be between 0 and 1."))

        # Surface whether a key is actually reachable, so nobody discovers a
        # missing key only when a background job fails at 2am.
        if self.provider == "anthropic":
            self.api_key_status = (
                "Key found in site_config.json"
                if frappe.conf.get("anthropic_api_key")
                else "MISSING - set anthropic_api_key in site_config.json"
            )
        else:
            self.api_key_status = "Not required for the 'mock' provider"
