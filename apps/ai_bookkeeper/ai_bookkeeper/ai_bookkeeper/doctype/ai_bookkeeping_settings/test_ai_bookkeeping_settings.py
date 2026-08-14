"""Credential handling for the provider key.

The point of these tests is not that the key can be saved -- it is *where* it
ends up. A credential that reaches the DocType's own table is a credential in
every backup dump, fixtures export and version record.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ai_bookkeeper.api import provider_status
from ai_bookkeeper.settings import get_api_key

SENTINEL = "sk-ant-test-DOCTYPE-STORED"
CONFIG_KEY = "sk-ant-test-FROM-SITE-CONFIG"


class TestAIBookkeepingSettings(FrappeTestCase):
    def setUp(self):
        super().setUp()
        self.settings = frappe.get_single("AI Bookkeeping Settings")
        self.settings.anthropic_api_key = SENTINEL
        self.settings.flags.ignore_permissions = True
        self.settings.save(ignore_permissions=True)
        frappe.db.commit()
        # Make sure no ambient site_config key masks the DocType path.
        self._saved_conf = frappe.conf.get("anthropic_api_key")
        frappe.conf["anthropic_api_key"] = None

    def tearDown(self):
        if self._saved_conf is None:
            frappe.conf.pop("anthropic_api_key", None)
        else:
            frappe.conf["anthropic_api_key"] = self._saved_conf
        super().tearDown()

    def test_key_is_not_stored_in_the_doctype_table(self):
        """The whole justification for allowing UI entry at all."""
        rows = frappe.db.sql(
            """SELECT value FROM `tabSingles`
               WHERE doctype = 'AI Bookkeeping Settings'
                 AND field = 'anthropic_api_key'""",
        )
        stored = rows[0][0] if rows else None
        self.assertNotEqual(
            stored, SENTINEL, "the raw key must never sit in the Singles table"
        )

    def test_key_is_stored_encrypted_in_auth_table(self):
        rows = frappe.db.sql(
            """SELECT `password` FROM `__Auth`
               WHERE doctype = 'AI Bookkeeping Settings'
                 AND fieldname = 'anthropic_api_key'""",
        )
        self.assertTrue(rows, "expected an __Auth row for the key")
        self.assertNotEqual(
            rows[0][0], SENTINEL, "the stored value must be encrypted, not plaintext"
        )

    def test_key_round_trips_through_get_password(self):
        key, source = get_api_key()
        self.assertEqual(key, SENTINEL)
        self.assertEqual(source, "AI Bookkeeping Settings")

    def test_site_config_wins_over_the_doctype(self):
        """Deployment config must not be overridable from a Settings page."""
        frappe.conf["anthropic_api_key"] = CONFIG_KEY
        key, source = get_api_key()
        self.assertEqual(key, CONFIG_KEY)
        self.assertEqual(source, "site_config.json")

    def test_status_reports_the_resolved_source(self):
        status = provider_status("anthropic")
        self.assertTrue(status["ok"])
        self.assertIn("AI Bookkeeping Settings", status["short"])

    def test_status_is_honest_when_no_key_anywhere(self):
        self.settings.anthropic_api_key = ""
        self.settings.save(ignore_permissions=True)
        frappe.db.commit()

        status = provider_status("anthropic")
        self.assertFalse(status["ok"])
        self.assertIn("MISSING", status["short"])

    def test_mock_provider_never_requires_a_key(self):
        status = provider_status("mock")
        self.assertTrue(status["ok"])
        self.assertIn("mock", status["short"])
