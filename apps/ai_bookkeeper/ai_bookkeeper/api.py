"""Whitelisted endpoints.

Everything here is permission-checked against the Accounts roles. Import and
re-categorisation both change what a reviewer sees, so neither is open to an
arbitrary logged-in user.
"""

from __future__ import annotations

import frappe
from frappe import _

from ai_bookkeeper.importer import import_csv_text
from ai_bookkeeper.pipeline import categorize_transaction, enqueue_categorization


def _require_accounts_role() -> None:
    roles = set(frappe.get_roles())
    if not roles & {"Accounts User", "Accounts Manager", "System Manager"}:
        frappe.throw(_("Not permitted."), frappe.PermissionError)


@frappe.whitelist()
def provider_status(provider: str | None = None) -> dict:
    """Whether the selected provider is actually usable right now.

    Exists because the stored ``api_key_status`` field is only recomputed in
    validate(), i.e. on save -- so switching the Provider select left it
    showing the previous provider's verdict, which reads as a broken field.
    This lets the form answer the question the moment the choice changes,
    without saving anything.

    Deliberately reports only presence, never any part of the key itself.
    """
    provider = (provider or "mock").strip().lower()

    if provider == "mock":
        return {
            "ok": True,
            "short": "Not required for the 'mock' provider",
            "message": (
                "Offline provider selected. Deterministic, no API key and no "
                "network -- rules still run and anything they cannot claim "
                "routes to a human."
            ),
        }

    if provider == "anthropic":
        from ai_bookkeeper.settings import get_api_key

        key, source = get_api_key()
        if key:
            return {
                "ok": True,
                "short": f"Key found in {source}",
                "message": (
                    f"Anthropic key resolved from <b>{frappe.utils.escape_html(source)}</b>. "
                    "site_config.json always wins when both are set."
                ),
            }
        return {
            "ok": False,
            "short": "MISSING - no key in site_config.json or this form",
            "message": (
                "No Anthropic key configured. Either set "
                "<code>anthropic_api_key</code> in site_config.json (preferred "
                "for deployments) or enter one in the field above. Until then "
                "the AI step fails and every unmatched row routes to review "
                "rather than being guessed at."
            ),
        }

    return {
        "ok": False,
        "short": f"Unknown provider {provider!r}",
        "message": f"Unknown provider {provider!r}. Supported: mock, anthropic.",
    }


@frappe.whitelist()
def import_transactions(csv_text: str, company: str | None = None, source_file: str = "upload"):
    """Import a bank CSV. Re-importing the same rows is a no-op."""
    _require_accounts_role()
    summary = import_csv_text(csv_text, company=company, source_file=source_file)
    return summary.as_dict()


@frappe.whitelist()
def import_attached_file(file_url: str, company: str | None = None):
    """Import from a File already attached to the site."""
    _require_accounts_role()
    file_doc = frappe.get_doc("File", {"file_url": file_url})
    content = file_doc.get_content()
    if isinstance(content, bytes):
        content = content.decode("utf-8-sig")
    summary = import_csv_text(content, company=company, source_file=file_doc.file_name)
    return summary.as_dict()


@frappe.whitelist()
def recategorize(transaction: str, background: bool = True):
    """Re-run categorisation for one row, e.g. after editing the rules."""
    _require_accounts_role()
    if background:
        enqueue_categorization(transaction, force=True)
        return {"queued": True, "transaction": transaction}
    return {"queued": False, "state": categorize_transaction(transaction, force=True)}


@frappe.whitelist()
def review_queue(company: str | None = None):
    """Everything currently waiting on a human, worst first."""
    _require_accounts_role()
    filters = {"workflow_state": ["in", ["Pending Review", "Needs Info"]]}
    if company:
        filters["company"] = company

    return frappe.get_all(
        "Staged Bank Transaction",
        filters=filters,
        fields=[
            "name", "txn_date", "description", "amount", "direction",
            "workflow_state", "proposed_account", "confidence",
            "review_reason", "journal_entry",
        ],
        order_by="confidence asc, txn_date asc",
    )


@frappe.whitelist()
def decision_audit(company: str | None = None):
    """AI proposal vs human decision, for the whole book."""
    _require_accounts_role()
    filters = {}
    if company:
        filters["company"] = company

    return frappe.get_all(
        "AI Categorization Log",
        filters=filters,
        fields=[
            "name", "transaction", "source", "tier", "confidence",
            "proposed_account", "final_account", "human_decision",
            "was_overridden", "decided_by", "decided_at", "model", "prompt_version",
        ],
        order_by="creation desc",
    )
