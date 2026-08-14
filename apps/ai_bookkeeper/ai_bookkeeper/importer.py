"""CSV import into Staged Bank Transaction.

Idempotency is the property worth caring about here. Bank statements get
re-downloaded and re-imported constantly -- overlapping date ranges, a retry
after a timeout, someone uploading the same file twice. Every one of those must
be a no-op rather than a duplicated expense in the ledger.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from io import StringIO

import frappe
from frappe import _

from ai_bookkeeper.core import ingest
from ai_bookkeeper.pipeline import enqueue_categorization


@dataclass
class ImportSummary:
    created: int = 0
    skipped_duplicate: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "created": self.created,
            "skipped_duplicate": self.skipped_duplicate,
            "failed": self.failed,
            "errors": self.errors,
            "names": self.names,
        }


def import_csv_text(
    text: str,
    company: str | None = None,
    source_file: str = "upload",
    enqueue: bool = True,
) -> ImportSummary:
    from ai_bookkeeper.settings import get_company

    company = company or get_company()
    if not company:
        frappe.throw(_("No company configured. Set one in AI Bookkeeping Settings."))

    summary = ImportSummary()

    for line_no, txn, error in ingest.parse_rows(StringIO(text)):
        if error:
            summary.failed += 1
            summary.errors.append(f"line {line_no}: {error}")
            continue

        if frappe.db.exists("Staged Bank Transaction", {"dedupe_hash": txn.dedupe_hash}):
            summary.skipped_duplicate += 1
            continue

        doc = frappe.new_doc("Staged Bank Transaction")
        doc.update(
            {
                "txn_date": txn.txn_date,
                "description": txn.description,
                "amount": float(txn.amount),
                "currency": txn.currency,
                "direction": txn.direction.value,
                "account_hint": txn.account_hint,
                "source_file": source_file,
                "dedupe_hash": txn.dedupe_hash,
                "company": company,
                "workflow_state": "Imported",
            }
        )
        doc.insert(ignore_permissions=True)
        summary.created += 1
        summary.names.append(doc.name)

        if enqueue:
            enqueue_categorization(doc.name)

    return summary


def import_csv_file(path: str, company: str | None = None, enqueue: bool = True) -> ImportSummary:
    with open(path, encoding="utf-8-sig", newline="") as handle:
        text = handle.read()
    return import_csv_text(
        text, company=company, source_file=os.path.basename(path), enqueue=enqueue
    )
