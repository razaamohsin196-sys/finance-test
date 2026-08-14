"""End-to-end tests against a real bench.

The pure-logic suite in ``ai_bookkeeper/tests`` proves the *decisions* are
right. This one proves the parts that only exist inside Frappe are right: that
re-import is a no-op, that nothing reaches the general ledger without a human
submitting, that a blocked row has no document to submit, and that the audit
log records an override as an override.

Run with:
    bench --site <site> run-tests --app ai_bookkeeper
"""

from __future__ import annotations

from pathlib import Path

import frappe
from frappe.tests.utils import FrappeTestCase

from ai_bookkeeper.importer import import_csv_file
from ai_bookkeeper.pipeline import categorize_transaction

SAMPLE = Path(__file__).parents[3] / "tests" / "sample_transactions_trial.csv"


class TestStagedBankTransaction(FrappeTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Ask for the configured company rather than "whichever Company exists".
        # Frappe's test runner creates its own `_Test Company N` records as
        # dependencies, and those have no usable chart of accounts -- so a bare
        # get_value("Company", {}) silently starts returning one of those the
        # moment another test module is added, and every posting test fails on
        # a missing root account.
        cls.company = frappe.db.get_single_value(
            "AI Bookkeeping Settings", "default_company"
        ) or frappe.db.get_value("Company", {"company_name": ("not like", "_Test%")}, "name")

        if not cls.company:
            raise RuntimeError(
                "No usable Company on this site. Run "
                "ai_bookkeeper.setup.bootstrap.ensure_company first."
            )

        frappe.db.set_single_value("AI Bookkeeping Settings", "provider", "mock")
        frappe.db.set_single_value("AI Bookkeeping Settings", "enable_llm", 1)
        frappe.db.set_single_value("AI Bookkeeping Settings", "confidence_threshold", 0.7)
        frappe.db.set_single_value("AI Bookkeeping Settings", "default_company", cls.company)

    def setUp(self):
        """Rebuild the fixture before every test.

        Several tests submit a Journal Entry, which mutates state every later
        test sees -- and unittest runs methods in alphabetical order, so
        "submitting" lands before "tier distribution" and moves a row out of
        AI Proposed. Rebuilding per test costs about two seconds and removes
        the entire class of order-dependent failures, rather than leaving the
        suite correct only for the current set of method names.
        """
        super().setUp()
        self.summary = self._load_fixture()
        self.names = self.summary.names

    @classmethod
    def _load_fixture(cls):
        """Import and categorise the sample from a clean slate."""
        from ai_bookkeeper.setup.bootstrap import reset_demo

        reset_demo()
        summary = import_csv_file(str(SAMPLE), company=cls.company, enqueue=False)
        for name in summary.names:
            categorize_transaction(name)
        frappe.db.commit()
        return summary

    # ------------------------------------------------------------------
    # Import
    # ------------------------------------------------------------------

    def test_imports_all_fifteen_rows(self):
        self.assertEqual(self.summary.created, 15)
        self.assertEqual(self.summary.failed, 0)

    def test_reimporting_the_same_file_is_a_no_op(self):
        again = import_csv_file(str(SAMPLE), company=self.company, enqueue=False)
        self.assertEqual(again.created, 0)
        self.assertEqual(again.skipped_duplicate, 15)
        self.assertEqual(frappe.db.count("Staged Bank Transaction"), 15)

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def test_tier_distribution_matches_the_pure_suite(self):
        counts = dict(
            frappe.db.sql(
                """SELECT workflow_state, COUNT(*) FROM `tabStaged Bank Transaction`
                   GROUP BY workflow_state"""
            )
        )
        self.assertEqual(counts.get("AI Proposed", 0), 5)
        self.assertEqual(counts.get("Pending Review", 0), 9)
        self.assertEqual(counts.get("Needs Info", 0), 1)

    def test_every_row_has_an_audit_log(self):
        self.assertEqual(frappe.db.count("AI Categorization Log"), 15)

    def test_flagged_rows_carry_a_reason(self):
        rows = frappe.get_all(
            "Staged Bank Transaction",
            filters={"workflow_state": ["in", ["Pending Review", "Needs Info"]]},
            fields=["name", "description", "review_reason"],
        )
        for row in rows:
            self.assertTrue(row.review_reason, f"{row.description} has no review_reason")

    # ------------------------------------------------------------------
    # The approval layer
    # ------------------------------------------------------------------

    def test_nothing_has_posted_to_the_ledger(self):
        """The whole point. Import + AI ran; the general ledger is untouched."""
        drafts = frappe.get_all(
            "Journal Entry", filters={"docstatus": 0}, pluck="name"
        )
        self.assertGreater(len(drafts), 0, "expected draft entries to exist")

        submitted = frappe.get_all(
            "Journal Entry", filters={"docstatus": 1}, pluck="name"
        )
        self.assertEqual(submitted, [], "no entry should be submitted without a human")

        gl = frappe.get_all(
            "GL Entry",
            filters={"voucher_no": ["in", [d for d in drafts]]},
        )
        self.assertEqual(gl, [], "a draft must not produce GL entries")

    def test_blocked_row_has_no_document_to_submit(self):
        blocked = frappe.get_all(
            "Staged Bank Transaction",
            filters={"workflow_state": "Needs Info"},
            fields=["name", "description", "journal_entry"],
        )
        self.assertEqual(len(blocked), 1)
        self.assertIn("UNKNOWN POS", blocked[0].description)
        self.assertFalse(
            blocked[0].journal_entry,
            "a Needs Info row must not have a draft that could be submitted",
        )

    def test_workflow_offers_no_path_from_needs_info_to_posted(self):
        transitions = frappe.get_all(
            "Workflow Transition",
            filters={"parent": "AI Bookkeeper Review", "state": "Needs Info"},
            fields=["next_state"],
        )
        reachable = {t.next_state for t in transitions}
        self.assertNotIn("Approved", reachable)
        self.assertNotIn("Posted", reachable)

    # ------------------------------------------------------------------
    # Closing the loop
    # ------------------------------------------------------------------

    def test_submitting_a_draft_records_approval(self):
        txn = frappe.get_all(
            "Staged Bank Transaction",
            filters={"workflow_state": "AI Proposed", "journal_entry": ["is", "set"]},
            fields=["name", "journal_entry", "proposed_account"],
            limit=1,
        )[0]

        entry = frappe.get_doc("Journal Entry", txn.journal_entry)
        entry.submit()
        frappe.db.commit()

        log = frappe.get_doc(
            "AI Categorization Log",
            frappe.db.get_value("AI Categorization Log", {"transaction": txn.name}, "name"),
        )
        self.assertEqual(log.human_decision, "approved")
        self.assertEqual(log.was_overridden, 0)
        self.assertEqual(log.final_account, txn.proposed_account)
        self.assertEqual(
            frappe.db.get_value("Staged Bank Transaction", txn.name, "workflow_state"),
            "Posted",
        )

    def test_editing_the_account_before_submit_is_recorded_as_an_override(self):
        txn = frappe.get_all(
            "Staged Bank Transaction",
            filters={
                "workflow_state": "AI Proposed",
                "journal_entry": ["is", "set"],
                "name": ["!=", ""],
            },
            fields=["name", "journal_entry", "proposed_account"],
            limit=2,
        )[-1]

        entry = frappe.get_doc("Journal Entry", txn.journal_entry)
        other = frappe.db.get_value(
            "Account",
            {
                "company": self.company,
                "is_group": 0,
                "root_type": "Expense",
                "name": ["!=", txn.proposed_account],
            },
            "name",
        )
        for row in entry.accounts:
            if row.account == txn.proposed_account:
                row.account = other
        entry.save()
        entry.submit()
        frappe.db.commit()

        log = frappe.get_doc(
            "AI Categorization Log",
            frappe.db.get_value("AI Categorization Log", {"transaction": txn.name}, "name"),
        )
        self.assertEqual(log.human_decision, "overridden")
        self.assertEqual(log.was_overridden, 1)
        self.assertEqual(log.final_account, other)
        self.assertNotEqual(log.final_account, log.proposed_account)

    def test_audit_log_refuses_to_rewrite_the_original_proposal(self):
        name = frappe.get_all("AI Categorization Log", pluck="name", limit=1)[0]
        log = frappe.get_doc("AI Categorization Log", name)
        log.rationale = "tampered"
        self.assertRaises(frappe.ValidationError, log.save)
