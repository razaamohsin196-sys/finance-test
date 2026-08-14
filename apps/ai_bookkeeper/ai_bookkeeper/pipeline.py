"""Orchestration: categorise a staged row, draft the entry, write the audit log.

The AI call happens in a background job (``frappe.enqueue``), never inline on
import. A bank statement import that blocks on fifteen sequential model calls
is a request timeout waiting to happen, and it couples the reliability of the
import to the availability of a third party.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import now_datetime

from ai_bookkeeper.converters import to_raw_transaction
from ai_bookkeeper.core.models import Decision, Source, Tier
from ai_bookkeeper.core.router import categorize
from ai_bookkeeper.posting import create_draft_journal_entry, resolve_account
from ai_bookkeeper.settings import get_provider, get_threshold

QUEUE = "long"

#: The routing tier and the workflow state are related but not the same thing:
#: the tier is a decision about a transaction, the state is a position in an
#: approval process that also has states no categoriser ever produces
#: (Approved, Posted, Rejected). Mapping them explicitly means renaming a
#: workflow state cannot silently change routing behaviour.
WORKFLOW_STATE_BY_TIER: dict[Tier, str] = {
    Tier.PROPOSED: "AI Proposed",
    Tier.PENDING_REVIEW: "Pending Review",
    Tier.NEEDS_INFO: "Needs Info",
}


def enqueue_categorization(txn_name: str, force: bool = False) -> None:
    frappe.enqueue(
        "ai_bookkeeper.pipeline.categorize_transaction",
        queue=QUEUE,
        job_id=f"ai-bookkeeper::categorize::{txn_name}",
        deduplicate=True,
        txn_name=txn_name,
        force=force,
    )


def categorize_transaction(txn_name: str, force: bool = False) -> str:
    """Categorise one staged transaction. Idempotent, safe to retry.

    Returns the resulting workflow state.
    """
    doc = frappe.get_doc("Staged Bank Transaction", txn_name)

    if doc.workflow_state in {"Approved", "Posted", "Rejected"} and not force:
        return doc.workflow_state
    if doc.workflow_state != "Imported" and not force:
        return doc.workflow_state

    txn = to_raw_transaction(doc)

    try:
        decision = categorize(txn, get_provider(), get_threshold())
    except Exception as exc:  # noqa: BLE001 - a categorisation crash must not lose the row
        frappe.log_error(
            title="AI Bookkeeper: categorisation failed",
            message=f"{txn_name}\n{frappe.get_traceback()}",
        )
        doc.db_set(
            {
                "categorization_error": str(exc)[:500],
                "workflow_state": "Pending Review",
                "review_reason": _(
                    "Automatic categorisation failed and needs manual handling: {0}"
                ).format(str(exc)[:300]),
            },
            update_modified=False,
        )
        return "Pending Review"

    _apply_decision(doc, decision)
    return doc.workflow_state


def _apply_decision(doc, decision: Decision) -> None:
    account = None
    if decision.account_key:
        account = resolve_account(doc.company, decision.account_key)

    journal_entry = None
    try:
        journal_entry = create_draft_journal_entry(doc, decision)
    except Exception as exc:  # noqa: BLE001
        frappe.log_error(
            title="AI Bookkeeper: draft creation failed",
            message=f"{doc.name}\n{frappe.get_traceback()}",
        )
        decision.review_reason = (
            (decision.review_reason or "")
            + f" Draft journal entry could not be created: {exc}"
        ).strip()
        decision.tier = Tier.PENDING_REVIEW

    doc.db_set(
        {
            "workflow_state": WORKFLOW_STATE_BY_TIER[decision.tier],
            "proposed_account": account,
            "proposed_account_key": decision.account_key,
            "confidence": decision.confidence,
            "ai_source": decision.source.value,
            "ai_model": decision.model,
            "rationale": decision.rationale,
            "review_reason": decision.review_reason,
            "journal_entry": journal_entry,
            "categorization_error": None,
        },
        update_modified=False,
    )

    _write_log(doc, decision, account, journal_entry)


def _write_log(doc, decision: Decision, account: str | None, journal_entry: str | None) -> None:
    """Record the proposal. This row is written once and never rewritten."""
    log = frappe.new_doc("AI Categorization Log")
    log.update(
        {
            "transaction": doc.name,
            "company": doc.company,
            "source": decision.source.value,
            "proposed_account": account,
            "proposed_account_key": decision.account_key,
            "confidence": decision.confidence,
            "tier": decision.tier.value,
            "model": decision.model,
            "prompt_version": decision.prompt_version,
            "rationale": decision.rationale,
            "review_reason": decision.review_reason,
            "raw_response": decision.raw_response,
            "human_decision": "needs_info" if decision.tier is Tier.NEEDS_INFO else "pending",
            "journal_entry": journal_entry,
        }
    )

    second = decision.second_opinion
    if second:
        log.update(
            {
                "shadow_account_key": second.account_key,
                "shadow_account": resolve_account(doc.company, second.account_key),
                "shadow_confidence": second.confidence,
                "shadow_rationale": second.rationale,
                "model_disagreed": 1 if decision.disagrees_with_model else 0,
                # The rule's answer stands; the raw response is kept either way
                # so the model's reasoning is recoverable later.
                "raw_response": decision.raw_response or second.raw_response,
            }
        )

    log.insert(ignore_permissions=True)


# --------------------------------------------------------------------------
# Closing the loop: what the human actually decided
# --------------------------------------------------------------------------


def on_journal_entry_submit(doc, method=None) -> None:
    """Called when a human submits a draft. This is the moment the books change.

    The comparison that matters is between the account the AI proposed and the
    account that actually posted -- not between what the AI proposed and what a
    button click said. A reviewer who edits the account and then submits has
    overridden the AI, and the log says so.
    """
    txn_name = frappe.db.get_value(
        "Staged Bank Transaction", {"journal_entry": doc.name}, "name"
    )
    if not txn_name:
        return

    txn = frappe.get_doc("Staged Bank Transaction", txn_name)
    posted_accounts = {row.account for row in doc.accounts}

    proposed = txn.proposed_account
    was_overridden = bool(proposed) and proposed not in posted_accounts

    final_account = proposed if not was_overridden else _pick_final_account(doc, txn)

    txn.db_set("workflow_state", "Posted", update_modified=False)

    log_name = frappe.db.get_value(
        "AI Categorization Log", {"transaction": txn_name}, "name", order_by="creation desc"
    )
    if not log_name:
        return

    log = frappe.get_doc("AI Categorization Log", log_name)
    log.db_set(
        {
            "human_decision": "overridden" if was_overridden else "approved",
            "final_account": final_account,
            "decided_by": frappe.session.user,
            "decided_at": now_datetime(),
            "was_overridden": 1 if was_overridden else 0,
            "journal_entry": doc.name,
        },
        update_modified=False,
    )


def _pick_final_account(journal_entry, txn) -> str | None:
    """The non-bank account that actually carried the classification."""
    bank_accounts = {
        row.account
        for row in journal_entry.accounts
        if frappe.get_cached_value("Account", row.account, "account_type") == "Bank"
    }
    for row in journal_entry.accounts:
        if row.account not in bank_accounts:
            return row.account
    return journal_entry.accounts[0].account if journal_entry.accounts else None


def on_journal_entry_cancel(doc, method=None) -> None:
    """A cancelled posting returns the row to review; the log keeps both facts."""
    txn_name = frappe.db.get_value(
        "Staged Bank Transaction", {"journal_entry": doc.name}, "name"
    )
    if not txn_name:
        return

    frappe.db.set_value(
        "Staged Bank Transaction", txn_name, "workflow_state", "Pending Review",
        update_modified=False,
    )

    log_name = frappe.db.get_value(
        "AI Categorization Log", {"transaction": txn_name}, "name", order_by="creation desc"
    )
    if log_name:
        frappe.db.set_value(
            "AI Categorization Log", log_name, "human_decision", "rejected",
            update_modified=False,
        )


# --------------------------------------------------------------------------
# Recovery
# --------------------------------------------------------------------------


def retry_failed_categorizations(limit: int = 50) -> int:
    """Re-queue rows that never got a proposal.

    A provider outage should be a delay, not a permanent hole in the ledger --
    rows that failed sit in review with an explanation and get picked up again
    once the provider is reachable.
    """
    stuck = frappe.get_all(
        "Staged Bank Transaction",
        filters={"workflow_state": "Imported"},
        pluck="name",
        limit=limit,
    )
    for name in stuck:
        enqueue_categorization(name, force=True)
    return len(stuck)
