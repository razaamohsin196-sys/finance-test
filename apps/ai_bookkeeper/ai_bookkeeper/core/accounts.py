"""Logical account keys.

The rules layer and the AI adapter both speak in these keys rather than in real
ERPNext account names. Two reasons:

1. The core stays testable without a site -- there is no chart of accounts to
   look up during a unit test.
2. The model is never asked to invent an account name. It picks from a closed
   vocabulary, which is the difference between a classification problem and a
   free-text generation problem. A hallucinated account name cannot reach the
   ledger because it is not a member of this set.

``ai_bookkeeper.posting`` maps each key onto a real account for the company.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AccountSpec:
    key: str
    #: Human label, also used as the ERPNext account name when we create it.
    label: str
    #: ERPNext root type: Asset / Liability / Income / Expense / Equity.
    root_type: str
    #: Short guidance shown to the model. Written for a reader who knows
    #: bookkeeping but has never seen this chart of accounts.
    guidance: str


ACCOUNTS: tuple[AccountSpec, ...] = (
    AccountSpec(
        "CLOUD_HOSTING",
        "Cloud Hosting",
        "Expense",
        "Infrastructure and hosting providers: AWS, GCP, Azure, DigitalOcean.",
    ),
    AccountSpec(
        "SOFTWARE_SUBSCRIPTIONS",
        "Software Subscriptions",
        "Expense",
        "Recurring SaaS billed monthly. Annual terms belong in PREPAID_EXPENSES.",
    ),
    AccountSpec(
        "CONTRACTOR_FEES",
        "Contractor Fees",
        "Expense",
        "Payments to non-employee contractors and freelancers.",
    ),
    AccountSpec(
        "OFFICE_SUPPLIES",
        "Office Supplies",
        "Expense",
        "Consumable office goods and small equipment.",
    ),
    AccountSpec(
        "BANK_CHARGES",
        "Bank Charges",
        "Expense",
        "Fees charged by the bank itself: service fees, wire fees, overdraft.",
    ),
    AccountSpec(
        "PAYMENT_PROCESSING_FEES",
        "Payment Processing Fees",
        "Expense",
        "Fees retained by a payment processor such as Stripe or PayPal.",
    ),
    AccountSpec(
        "PREPAID_EXPENSES",
        "Prepaid Expenses",
        "Asset",
        "Amounts paid now for service delivered over a future period, such as "
        "an annual subscription. Recognised across the term, not on payment.",
    ),
    AccountSpec(
        "STRIPE_CLEARING",
        "Stripe Clearing",
        "Asset",
        "Holding account for processor payouts until the settlement report "
        "lets gross revenue and processor fees be separated.",
    ),
    AccountSpec(
        "SUSPENSE",
        "Suspense",
        "Asset",
        "Holding account for transactions that cannot be identified at all. "
        "Nothing should rest here; it exists so unknowns stay visible.",
    ),
    # Note these are holding accounts, not Debtors/Creditors themselves.
    # A ledger line against real Receivable or Payable has to name the customer
    # or supplier it belongs to -- ERPNext enforces that, and rightly so, since
    # an AR balance that isn't attributable to anyone is not an AR balance. The
    # rows that land here are exactly the ones where the counterparty is what
    # we don't know yet, so they park in a party-free holding account until a
    # human matches them to an invoice.
    AccountSpec(
        "UNAPPLIED_RECEIPTS",
        "Unapplied Customer Receipts",
        "Asset",
        "Money received from a customer that has not yet been matched to an "
        "invoice. Settles a receivable once matched -- it is not new income.",
    ),
    AccountSpec(
        "UNAPPLIED_PAYMENTS",
        "Unapplied Supplier Payments",
        "Asset",
        "Money paid to a supplier that has not yet been allocated to a bill.",
    ),
    AccountSpec(
        "SALES_REVENUE",
        "Sales",
        "Income",
        "Revenue earned from customers.",
    ),
    # Named to avoid colliding with ERPNext's stock "Employee Advances", which
    # ships typed as Payable and therefore needs a party on every line. The
    # rows that land here are flagged precisely because the claimant and the
    # nature of the payment are still unknown.
    AccountSpec(
        "UNSUBSTANTIATED_REIMBURSEMENTS",
        "Unsubstantiated Reimbursements",
        "Asset",
        "Money paid to an individual that looks like a reimbursement but has "
        "no supporting claim or receipt yet. Reclassified once substantiated.",
    ),
    AccountSpec(
        "BANK_PRIMARY",
        "Primary Checking",
        "Asset",
        "The operating bank account this statement belongs to.",
    ),
    AccountSpec(
        "BANK_SAVINGS",
        "Savings",
        "Asset",
        "A second bank account held by the same entity. Movement between own "
        "accounts is a transfer, never income or expense.",
    ),
)

BY_KEY: dict[str, AccountSpec] = {spec.key: spec for spec in ACCOUNTS}

#: The closed vocabulary handed to the model.
ALLOWED_KEYS: tuple[str, ...] = tuple(spec.key for spec in ACCOUNTS)


def is_valid(key: str | None) -> bool:
    return key in BY_KEY


def guidance_block() -> str:
    """Render the account vocabulary for the model prompt."""
    return "\n".join(f"- {s.key} ({s.root_type}): {s.guidance}" for s in ACCOUNTS)
