"""Turning a routed ``Decision`` into a draft Journal Entry.

Two invariants this module exists to hold:

1. **Everything it creates is a draft.** ``docstatus`` is left at 0 and nothing
   here calls ``.submit()``. Posting to the general ledger is a human action,
   performed in ERPNext's own UI, using ERPNext's own draft/submit semantics --
   not a bespoke "approved" flag this app invented and could get wrong.
2. **Needs Info produces no document at all** (unless a site explicitly opts in
   to a Suspense draft). The strongest guarantee that something cannot post is
   that there is nothing to submit.
"""

from __future__ import annotations

import frappe
from frappe import _

from ai_bookkeeper.core.accounts import BY_KEY
from ai_bookkeeper.core.models import Decision, Tier


def _account_cache() -> dict[tuple[str, str], str]:
    """Per-request cache of (company, account_key) -> account name.

    Scoped to ``frappe.local`` rather than module level on purpose: a background
    worker is a long-lived process, and a module-level dict would keep serving
    an account name after the account had been renamed or the cache had been
    populated for a different site in the same worker.
    """
    if not hasattr(frappe.local, "_ai_bookkeeper_accounts"):
        frappe.local._ai_bookkeeper_accounts = {}
    return frappe.local._ai_bookkeeper_accounts


def _abbr(company: str) -> str:
    return frappe.get_cached_value("Company", company, "abbr")


def resolve_account(company: str, account_key: str) -> str:
    """Map a logical account key onto a real ERPNext account.

    Resolution order: an explicit override on the Vendor Account Rule chart
    mapping, then an account whose name matches the spec label, then creation
    under the right root type. Creating is deliberate -- a trial bench starts
    from a stock chart of accounts that has no "Stripe Clearing", and failing
    the whole import over a missing account helps nobody.
    """
    cache = _account_cache()
    cache_key = (company, account_key)
    if cache_key in cache:
        return cache[cache_key]

    spec = BY_KEY.get(account_key)
    if not spec:
        frappe.throw(_("Unknown account key: {0}").format(account_key))

    abbr = _abbr(company)
    expected = f"{spec.label} - {abbr}"

    if frappe.db.exists("Account", expected):
        cache[cache_key] = expected
        return expected

    # Stock ERPNext charts vary in naming; fall back to a leaf account with the
    # right root type and a matching label before creating a new one.
    existing = frappe.db.get_value(
        "Account",
        {
            "company": company,
            "account_name": spec.label,
            "is_group": 0,
        },
        "name",
    )
    if existing:
        cache[cache_key] = existing
        return existing

    account = _create_account(company, spec, abbr)
    cache[cache_key] = account
    return account


def _create_account(company: str, spec, abbr: str) -> str:
    parent = frappe.db.get_value(
        "Account",
        {"company": company, "is_group": 1, "root_type": spec.root_type, "parent_account": ("is", "not set")},
        "name",
    ) or frappe.db.get_value(
        "Account", {"company": company, "is_group": 1, "root_type": spec.root_type}, "name"
    )

    if not parent:
        frappe.throw(
            _("No {0} group account found for company {1}. Set up a chart of accounts first.").format(
                spec.root_type, company
            )
        )

    doc = frappe.get_doc(
        {
            "doctype": "Account",
            "account_name": spec.label,
            "company": company,
            "parent_account": parent,
            "root_type": spec.root_type,
            "is_group": 0,
            "account_type": _account_type_for(spec),
        }
    )
    doc.insert(ignore_permissions=True)
    return doc.name


def _account_type_for(spec) -> str | None:
    return {
        "BANK_PRIMARY": "Bank",
        "BANK_SAVINGS": "Bank",
        # Deliberately NOT mapped to "Receivable"/"Payable": those account
        # types require a party on every ledger line, and the whole reason
        # these rows are flagged is that the party is unknown.
    }.get(spec.key)


def _reject_party_accounts(account: str, account_key: str) -> None:
    """Refuse to draft a ledger line that ERPNext would reject anyway.

    A line against a Receivable or Payable account must name the customer or
    supplier it belongs to -- ERPNext enforces this, and it is right to: an AR
    balance nobody owes is not an AR balance. Every row that would want such an
    account here is flagged precisely *because* the counterparty is unknown, so
    the vocabulary routes them to party-free holding accounts instead (see
    ``core.accounts``).

    This is the belt to that braces: if a future rule or chart mapping ever
    points at a real party account, fail loudly at draft time rather than
    producing a document that cannot be submitted.
    """
    account_type = frappe.get_cached_value("Account", account, "account_type")
    if account_type in {"Receivable", "Payable"}:
        frappe.throw(
            _(
                "Account {0} (from key {1}) is a {2} account and needs a party on "
                "every ledger line. Map this key to a party-free holding account "
                "instead."
            ).format(account, account_key, account_type)
        )


def create_draft_journal_entry(txn_doc, decision: Decision) -> str | None:
    """Create the draft Journal Entry for a decision. Returns its name, or None.

    Returns ``None`` when the decision is structurally unpostable and the site
    has not opted in to Suspense drafts -- there is deliberately no document to
    submit in that case.
    """
    from ai_bookkeeper.settings import should_draft_needs_info

    if decision.tier is Tier.NEEDS_INFO and not should_draft_needs_info():
        return None

    if not decision.lines:
        return None

    company = txn_doc.company
    entry = frappe.new_doc("Journal Entry")
    entry.voucher_type = "Journal Entry"
    entry.company = company
    entry.posting_date = txn_doc.txn_date
    entry.user_remark = _build_remark(txn_doc, decision)
    entry.cheque_no = txn_doc.name
    entry.cheque_date = txn_doc.txn_date

    for line in decision.lines:
        account = resolve_account(company, line.account_key)
        _reject_party_accounts(account, line.account_key)
        entry.append(
            "accounts",
            {
                "account": account,
                "debit_in_account_currency": float(line.debit),
                "credit_in_account_currency": float(line.credit),
                "user_remark": line.remark or None,
            },
        )

    entry.flags.ignore_permissions = True
    entry.insert(ignore_permissions=True)
    # No .submit(). That is the human's call, in ERPNext's own UI.
    return entry.name


def _build_remark(txn_doc, decision: Decision) -> str:
    """The remark a reviewer reads first. Written for a human, not a log parser."""
    parts = [
        f"Imported bank line: {txn_doc.description}",
        f"Proposed by: {decision.source.value}"
        + (f" ({decision.model})" if decision.model else ""),
        f"Confidence: {decision.confidence:.2f}",
    ]
    if decision.rationale:
        parts.append(f"Rationale: {decision.rationale}")
    if decision.review_reason:
        parts.append(f"NEEDS A HUMAN: {decision.review_reason}")
    parts.append(
        "This entry is a draft. Review the accounts and amounts, then submit to post."
    )
    return "\n".join(parts)
