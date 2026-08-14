"""The deterministic layer. Runs before the model, and outranks it.

Two kinds of rule live here, and the order between them is the whole point:

* **Guards** run first. They encode the cases where a confident answer is the
  wrong answer -- a contradictory sign, an unsettled charge, a payout whose
  fees are not disclosed. A guard can hand back a proposal, but it always
  attaches a ``review_reason`` and it never lets the row through unattended.
* **Vendor rules** run second. Plain pattern-to-account mappings for traffic
  that genuinely is unambiguous (AWS, Zoom, bank fees).

Anything neither kind claims falls through to the model. That ordering is what
keeps the model cheap (it sees a minority of rows), deterministic where it
matters (a rule can't drift between runs) and auditable (a rule match names the
rule that fired).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from decimal import Decimal

from .models import Decision, Direction, JournalLine, RawTransaction, Source, Tier

#: Confidence attached to a deterministic match. Not 1.0: a regex matching a
#: vendor string is strong evidence about *who* was paid, not proof about *what
#: for*. Reserving the top of the scale keeps the number meaningful.
RULE_CONFIDENCE = 0.95

#: Names that, if they appear as a payee, warrant a related-party note. In a
#: real deployment this comes from the company's officers and shareholders;
#: it is seeded here so the behaviour is visible in the trial.
RELATED_PARTY_NAMES: tuple[str, ...] = ("RAYYAN",)

_GROSS_FEE = re.compile(
    r"GROSS\s+(?P<gross>[\d,]+(?:\.\d+)?)\s+FEE\s+(?P<fee>[\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)
_PROCESSOR_PAYOUT = re.compile(r"\b(STRIPE|PAYPAL|SQUARE|ADYEN)\b.*\bPAYOUT\b", re.IGNORECASE)
_INTERNAL_TRANSFER = re.compile(
    r"\bTRANSFER\s+(?:FROM|TO)\s+(?P<counterparty>SAVINGS|CHECKING|MONEY\s+MARKET)\b",
    re.IGNORECASE,
)
_INVOICE_REF = re.compile(r"\bINV[\s#-]*(?P<ref>[\w-]+)", re.IGNORECASE)
_REFUND = re.compile(r"\bREFUND\b", re.IGNORECASE)
_CUSTOMER_PAYMENT = re.compile(r"\b(CLIENT|CUSTOMER)\s+(DEPOSIT|PAYMENT)\b", re.IGNORECASE)
_UNSETTLED = re.compile(r"\b(PENDING|AUTHORI[SZ]ATION\s+ONLY|TEMP\s+AUTH)\b", re.IGNORECASE)
_UNIDENTIFIED_MERCHANT = re.compile(r"\bUNKNOWN\b|\bUNIDENTIFIED\b", re.IGNORECASE)
_REIMBURSEMENT = re.compile(r"\bREIMBURSE(?:MENT|D)?\b", re.IGNORECASE)
_ANNUAL_TERM = re.compile(r"\b(ANNUAL|YEARLY|12\s*-?\s*MONTH|1\s*YR|YR\s*PLAN)\b", re.IGNORECASE)
_GENERIC_VENDOR_PAYMENT = re.compile(r"\bVENDOR\s+PAYMENT\b", re.IGNORECASE)
_PAYEE_NAME = re.compile(r"\b[A-Z]\.\s?[A-Z][A-Z]+\b")


def _d(raw: str) -> Decimal:
    return Decimal(raw.replace(",", ""))


def _two_line(txn: RawTransaction, counter_key: str, remark: str = "") -> list[JournalLine]:
    """Bank on one side, the categorised account on the other.

    ``direction`` is read from the file rather than inferred from the sign, so
    a row whose sign contradicts its direction still produces a readable
    proposal for a human to correct rather than silently flipping.
    """
    amount = txn.magnitude
    if txn.direction is Direction.CREDIT:
        return [
            JournalLine("BANK_PRIMARY", debit=amount, remark=remark),
            JournalLine(counter_key, credit=amount, remark=remark),
        ]
    return [
        JournalLine(counter_key, debit=amount, remark=remark),
        JournalLine("BANK_PRIMARY", credit=amount, remark=remark),
    ]


# --------------------------------------------------------------------------
# Guards
# --------------------------------------------------------------------------


def guard_sign_contradiction(txn: RawTransaction) -> Decision | None:
    """A negative amount in a file that already carries a direction column.

    Row 10 of the sample is ``-350.00`` with ``direction=debit``. The sign is
    encoded twice and the two encodings disagree, so the true direction is
    genuinely unknown. Picking either reading is a coin flip on the sign of a
    ledger entry, so this always stops for a human -- and it stops *first*,
    ahead of the refund handling that would otherwise claim the row.
    """
    if not txn.has_sign_contradiction:
        return None

    extra = ""
    counter_key = "SUSPENSE"
    if _REFUND.search(txn.description):
        counter_key = "SALES_REVENUE"
        extra = (
            " Separately, the description reads as a customer refund, which "
            "belongs against revenue as a credit note rather than as an expense."
        )

    return Decision(
        tier=Tier.PENDING_REVIEW,
        source=Source.RULE,
        account_key=counter_key,
        confidence=0.0,
        rationale=(
            "Amount is negative while the direction column already says "
            f"'{txn.direction.value}'. The file encodes sign twice and the two "
            "disagree."
        ),
        review_reason=(
            "Sign contradiction: amount is negative but direction is "
            f"'{txn.direction.value}'. A human must confirm the true direction "
            "before this posts." + extra
        ),
        lines=_two_line(txn, counter_key, remark="direction unconfirmed"),
    )


def guard_unsettled_or_unknown(txn: RawTransaction) -> Decision | None:
    """Unidentifiable merchant, or a charge that has not settled.

    Row 15 is both: ``UNKNOWN POS 4455 - PENDING``. A pending authorisation is
    not yet a transaction, and an unknown merchant cannot be categorised at
    all. This is the one outcome the workflow refuses to post -- see
    ``Tier.NEEDS_INFO``.
    """
    unknown = bool(_UNIDENTIFIED_MERCHANT.search(txn.description))
    unsettled = bool(_UNSETTLED.search(txn.description))
    if not (unknown or unsettled):
        return None

    reasons = []
    if unknown:
        reasons.append("merchant cannot be identified from the description")
    if unsettled:
        reasons.append("the charge has not settled yet")

    return Decision(
        tier=Tier.NEEDS_INFO,
        source=Source.RULE,
        account_key="SUSPENSE",
        confidence=0.0,
        rationale="Cannot be categorised: " + " and ".join(reasons) + ".",
        review_reason=(
            "Blocked: " + " and ".join(reasons) + ". Needs the merchant "
            "identified and the charge settled before it can be booked."
        ),
        lines=_two_line(txn, "SUSPENSE", remark="unidentified"),
    )


def guard_processor_payout(txn: RawTransaction) -> Decision | None:
    """Payment-processor payouts. Two shapes, two different problems.

    *With* a gross/fee breakdown (row 14): the payout must be split, or both
    revenue and processor fees are understated by the fee amount. The rule also
    checks that ``gross - fee`` actually reconciles to the net deposited, since
    a breakdown that does not tie is worse than none at all.

    *Without* one (row 1): the fees are real but undisclosed, so the split is
    not derivable from the bank line. The honest treatment is to park the net
    in a clearing account until the settlement report arrives -- not to book
    the net as revenue and quietly lose the fee.
    """
    if not _PROCESSOR_PAYOUT.search(txn.description):
        return None

    match = _GROSS_FEE.search(txn.description)
    if not match:
        return Decision(
            tier=Tier.PENDING_REVIEW,
            source=Source.RULE,
            account_key="STRIPE_CLEARING",
            confidence=0.4,
            rationale=(
                "Processor payout with no gross/fee breakdown in the "
                "description. The net is known; the fee withheld is not."
            ),
            review_reason=(
                "Processor payout arrived net, with fees not disclosed on the "
                "bank line. Booked to clearing pending the settlement report "
                "-- the gross/fee split cannot be derived from this row alone."
            ),
            lines=_two_line(txn, "STRIPE_CLEARING", remark="net payout, fees undisclosed"),
        )

    gross = _d(match.group("gross"))
    fee = _d(match.group("fee"))
    net = txn.magnitude
    reconciles = (gross - fee) == net

    lines = [
        JournalLine("BANK_PRIMARY", debit=net, remark="net payout received"),
        JournalLine("PAYMENT_PROCESSING_FEES", debit=fee, remark="processor fee withheld"),
        JournalLine("STRIPE_CLEARING", credit=gross, remark="gross settled by processor"),
    ]

    if not reconciles:
        return Decision(
            tier=Tier.PENDING_REVIEW,
            source=Source.RULE,
            account_key="STRIPE_CLEARING",
            confidence=0.0,
            rationale=(
                f"Stated gross {gross} less fee {fee} is {gross - fee}, which "
                f"does not equal the net {net} actually deposited."
            ),
            review_reason=(
                f"Payout does not reconcile: gross {gross} - fee {fee} = "
                f"{gross - fee}, but {net} was deposited. Do not post until the "
                "discrepancy is explained."
            ),
            lines=lines,
        )

    return Decision(
        tier=Tier.PENDING_REVIEW,
        source=Source.RULE,
        account_key="STRIPE_CLEARING",
        confidence=0.9,
        rationale=(
            f"Payout discloses gross {gross} and fee {fee}, reconciling to the "
            f"net {net}. Split into three lines so neither gross settlement nor "
            "the processor fee is lost."
        ),
        review_reason=(
            "Multi-line split proposed (gross vs fee). Confirm the gross belongs "
            "against clearing rather than direct revenue, given how invoices "
            "were raised."
        ),
        lines=lines,
    )


def guard_internal_transfer(txn: RawTransaction) -> Decision | None:
    """Movement between the entity's own accounts. Never income, never expense.

    A naive categoriser books row 5 (``TRANSFER FROM SAVINGS``, 2000 credit) as
    revenue, which inflates the top line by the transfer amount. The
    counterparty account still has to be confirmed by a human -- the masked
    digits identify it to the account holder, not to us.
    """
    match = _INTERNAL_TRANSFER.search(txn.description)
    if not match:
        return None

    counterparty = match.group("counterparty").upper().replace(" ", "_")
    counter_key = "BANK_SAVINGS" if "SAVING" in counterparty else "BANK_PRIMARY"

    return Decision(
        tier=Tier.PENDING_REVIEW,
        source=Source.RULE,
        account_key=counter_key,
        confidence=0.85,
        rationale=(
            "Movement between the entity's own bank accounts. This is a balance "
            "sheet transfer with no effect on income or expense."
        ),
        review_reason=(
            "Internal transfer, not income. Confirm which of the company's own "
            "accounts the masked counterparty refers to before posting."
        ),
        lines=_two_line(txn, counter_key, remark="internal transfer"),
    )


def guard_customer_settlement(txn: RawTransaction) -> Decision | None:
    """Money in that references an invoice: settles a receivable, not new income.

    Row 8 (``CLIENT DEPOSIT - INV 2025-118``) has already been recognised as
    revenue when the invoice was raised. Booking it to income again double
    counts. Needs a human to match it to the actual Sales Invoice.
    """
    if txn.direction is not Direction.CREDIT:
        return None
    invoice = _INVOICE_REF.search(txn.description)
    if not (invoice or _CUSTOMER_PAYMENT.search(txn.description)):
        return None

    ref = invoice.group("ref") if invoice else None
    ref_text = f" referencing invoice {ref}" if ref else ""

    return Decision(
        tier=Tier.PENDING_REVIEW,
        source=Source.RULE,
        account_key="UNAPPLIED_RECEIPTS",
        confidence=0.8,
        rationale=(
            f"Incoming customer payment{ref_text}. Revenue was recognised when "
            "the invoice was raised, so this settles the receivable rather than "
            "creating new income."
        ),
        review_reason=(
            f"Needs matching to the open Sales Invoice{ref_text}. Booking this "
            "to income would double count revenue already recognised."
        ),
        lines=_two_line(txn, "UNAPPLIED_RECEIPTS", remark=f"settles {ref}" if ref else "settles AR"),
    )


def guard_customer_refund(txn: RawTransaction) -> Decision | None:
    """Money out that references a refund: contra-revenue, not an expense."""
    if not _REFUND.search(txn.description):
        return None

    invoice = _INVOICE_REF.search(txn.description)
    ref = invoice.group("ref") if invoice else None
    ref_text = f" against invoice {ref}" if ref else ""

    return Decision(
        tier=Tier.PENDING_REVIEW,
        source=Source.RULE,
        account_key="SALES_REVENUE",
        confidence=0.75,
        rationale=(
            f"Customer refund{ref_text}. Reduces revenue via a credit note; it "
            "is not a cost of doing business."
        ),
        review_reason=(
            f"Refund{ref_text} should be raised as a credit note against the "
            "original invoice, not booked as an expense."
        ),
        lines=_two_line(txn, "SALES_REVENUE", remark=f"refund {ref}" if ref else "customer refund"),
    )


def guard_reimbursement(txn: RawTransaction) -> Decision | None:
    """A payment to a person, labelled as a possible reimbursement.

    Row 12 is ``J. MARTINEZ - REIMBURSEMENT?`` -- the question mark is in the
    source data. Expense claim, vendor payment and salary advance all post
    differently and have different tax treatment, and nothing on the bank line
    distinguishes them. There is no underlying receipt to check against.
    """
    if not _REIMBURSEMENT.search(txn.description):
        return None

    return Decision(
        tier=Tier.PENDING_REVIEW,
        source=Source.RULE,
        account_key="UNSUBSTANTIATED_REIMBURSEMENTS",
        confidence=0.3,
        rationale=(
            "Payment to an individual described as a reimbursement, with no "
            "supporting document referenced."
        ),
        review_reason=(
            "Ambiguous: could be an employee expense claim, a vendor payment, "
            "or a salary advance -- each posts differently and is taxed "
            "differently. Needs the underlying receipt or claim before posting."
        ),
        lines=_two_line(txn, "UNSUBSTANTIATED_REIMBURSEMENTS", remark="pending substantiation"),
    )


def guard_unallocatable_payment(txn: RawTransaction) -> Decision | None:
    """Outbound payment that names no payee and no invoice.

    Row 13 is ``WISE TRANSFER - VENDOR PAYMENT``: we know money left, and that
    it went to a supplier, and nothing else. There is no expense account that
    can be chosen honestly, so it goes to payables for a human to allocate.
    """
    if txn.direction is not Direction.DEBIT:
        return None
    if not _GENERIC_VENDOR_PAYMENT.search(txn.description):
        return None
    if _INVOICE_REF.search(txn.description) or _PAYEE_NAME.search(txn.description):
        return None

    return Decision(
        tier=Tier.PENDING_REVIEW,
        source=Source.RULE,
        account_key="UNAPPLIED_PAYMENTS",
        confidence=0.35,
        rationale=(
            "Supplier payment with neither a payee name nor an invoice "
            "reference on the bank line."
        ),
        review_reason=(
            "No payee and no invoice reference, so no expense account can be "
            "chosen honestly. Needs allocation against a supplier invoice."
        ),
        lines=_two_line(txn, "UNAPPLIED_PAYMENTS", remark="unallocated supplier payment"),
    )


def guard_annual_prepayment(txn: RawTransaction) -> Decision | None:
    """An annual term paid up front is an asset, not this month's expense.

    Row 7 is ``PAYPAL *CANVA PRO ANNUAL``. Expensing all 119.00 in July
    overstates July and understates the next eleven months. Runs ahead of the
    vendor rules so a generic software mapping cannot claim it first.
    """
    if txn.direction is not Direction.DEBIT:
        return None
    if not _ANNUAL_TERM.search(txn.description):
        return None

    return Decision(
        tier=Tier.PENDING_REVIEW,
        source=Source.RULE,
        account_key="PREPAID_EXPENSES",
        confidence=0.8,
        rationale=(
            "Annual subscription term paid up front. The service is delivered "
            "over twelve months, so the cost is an asset on payment and is "
            "recognised across the term."
        ),
        review_reason=(
            "Annual prepayment: needs an amortisation schedule over the "
            "subscription term rather than a single-period expense."
        ),
        lines=_two_line(txn, "PREPAID_EXPENSES", remark="annual term, to be amortised"),
    )


GUARDS: tuple[Callable[[RawTransaction], Decision | None], ...] = (
    guard_sign_contradiction,
    guard_unsettled_or_unknown,
    guard_processor_payout,
    guard_internal_transfer,
    guard_annual_prepayment,
    guard_customer_settlement,
    guard_customer_refund,
    guard_reimbursement,
    guard_unallocatable_payment,
)


# --------------------------------------------------------------------------
# Vendor rules
# --------------------------------------------------------------------------

#: (regex, account key, human label). Seeded here and mirrored into the
#: ``Vendor Account Rule`` DocType at install time so a bookkeeper can extend
#: the map without a deploy.
VENDOR_RULES: tuple[tuple[str, str, str], ...] = (
    (r"\bAWS\b|AMAZON\s+WEB\s+SERVICES|\bGCP\b|AZURE|DIGITALOCEAN", "CLOUD_HOSTING", "Cloud provider"),
    (r"ZOOM\.US|\bZOOM\b|SLACK|NOTION|GITHUB|FIGMA|CANVA", "SOFTWARE_SUBSCRIPTIONS", "SaaS vendor"),
    (r"UPWORK|FIVERR|TOPTAL|FREELANCER\.COM", "CONTRACTOR_FEES", "Contractor marketplace"),
    (r"OFFICE\s?DEPOT|STAPLES|OFFICEMAX", "OFFICE_SUPPLIES", "Office supplier"),
    (r"BANK\s+SERVICE\s+FEE|MONTHLY\s+MAINTENANCE\s+FEE|WIRE\s+FEE|OVERDRAFT\s+FEE", "BANK_CHARGES", "Bank fee"),
)

_COMPILED_VENDOR_RULES = tuple(
    (re.compile(pattern, re.IGNORECASE), key, label) for pattern, key, label in VENDOR_RULES
)


def match_vendor(txn: RawTransaction) -> Decision | None:
    for pattern, account_key, label in _COMPILED_VENDOR_RULES:
        if not pattern.search(txn.description):
            continue

        rationale = f"{label} matched deterministically by pattern /{pattern.pattern}/."
        decision = Decision(
            tier=Tier.PROPOSED,
            source=Source.RULE,
            account_key=account_key,
            confidence=RULE_CONFIDENCE,
            rationale=rationale,
            lines=_two_line(txn, account_key, remark=label),
        )
        _annotate_related_party(txn, decision)
        return decision
    return None


def _annotate_related_party(txn: RawTransaction, decision: Decision) -> None:
    """Flag a payee that matches a known related party.

    A heuristic, and treated as one: it appends a disclosure note to the
    rationale and does not change the tier. Under a real related-party policy
    this would route to review; that policy needs the company's actual officer
    and shareholder list, which a trial fixture does not have.
    """
    upper = txn.description.upper()
    for name in RELATED_PARTY_NAMES:
        if name in upper:
            decision.rationale += (
                f" Note: payee string contains '{name}', which matches a known "
                "related party. Flagged for disclosure, not blocked."
            )
            return


def apply(txn: RawTransaction) -> Decision | None:
    """Run the deterministic layer. ``None`` means 'ask the model'."""
    for guard in GUARDS:
        decision = guard(txn)
        if decision is not None:
            _annotate_related_party(txn, decision)
            return decision
    return match_vendor(txn)
