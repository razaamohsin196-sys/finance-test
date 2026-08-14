"""Value types shared by the rules layer, the AI adapter and the router.

Nothing in ``ai_bookkeeper.core`` imports frappe. That is deliberate: the
categorisation logic is the part worth testing hard, and keeping it free of the
ORM means the whole 15-row fixture runs in plain pytest with no bench, no
database and no network.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum


class Direction(str, Enum):
    DEBIT = "debit"
    CREDIT = "credit"


class Tier(str, Enum):
    """Where a transaction lands after categorisation.

    Every tier produces a *draft*. Nothing here posts to the general ledger on
    its own -- the tier only decides how much human attention the draft needs
    and, for NEEDS_INFO, whether it is allowed to reach the ledger at all.
    """

    #: Rule matched, or model confidence met the threshold. Routine review.
    PROPOSED = "Proposed"
    #: Explicitly flagged. Carries a human-readable ``review_reason``.
    PENDING_REVIEW = "Pending Review"
    #: Structurally blocked. The workflow has no transition from here to Posted.
    NEEDS_INFO = "Needs Info"


class Source(str, Enum):
    RULE = "rule"
    LLM = "llm"
    #: The model was unreachable or returned something unusable.
    FALLBACK = "fallback"


@dataclass(frozen=True)
class RawTransaction:
    """One row of the bank export, before any interpretation."""

    txn_date: date
    description: str
    amount: Decimal
    currency: str
    direction: Direction
    account_hint: str | None = None

    @property
    def dedupe_hash(self) -> str:
        """Stable identity for a row, so re-importing the same file is a no-op.

        Deliberately excludes ``account_hint``: the hint is advisory metadata
        that a bank might start or stop populating between exports, and a row
        does not become a different transaction because of it.
        """
        payload = "|".join(
            [
                self.txn_date.isoformat(),
                self.description.strip().upper(),
                f"{self.amount:.2f}",
                self.direction.value,
                self.currency.upper(),
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @property
    def magnitude(self) -> Decimal:
        return abs(self.amount)

    @property
    def has_sign_contradiction(self) -> bool:
        """True when the row encodes its sign twice and the two disagree.

        This file carries direction in its own column, so ``amount`` is a
        magnitude and should never be negative. A negative amount alongside an
        explicit direction is a contradiction, not a convention -- resolving it
        silently either way is a coin flip on the sign of a ledger entry.
        """
        return self.amount < 0


@dataclass(frozen=True)
class JournalLine:
    """One side of a proposed journal entry, in logical-account terms.

    ``account_key`` is a logical name (see ``core.accounts``) rather than a real
    ERPNext account. The Frappe layer resolves it against the company's chart of
    accounts, which keeps the rules readable and the core free of site state.
    """

    account_key: str
    debit: Decimal = Decimal("0")
    credit: Decimal = Decimal("0")
    remark: str = ""


@dataclass
class Decision:
    """The full outcome for one transaction: what, how sure, and why."""

    tier: Tier
    source: Source
    account_key: str | None = None
    confidence: float = 0.0
    rationale: str = ""
    review_reason: str | None = None
    #: Populated when the entry needs more than a simple two-line posting.
    lines: list[JournalLine] = field(default_factory=list)
    #: Verbatim provider response, kept for the audit trail.
    raw_response: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    #: When a rule decided the outcome, the model is still asked separately and
    #: its answer recorded here. It never changes the decision -- it exists so
    #: every row carries an AI proposal, and so rule/model disagreement is
    #: visible instead of silent. See ``core.router.categorize``.
    second_opinion: "ProviderResult | None" = None

    @property
    def is_postable(self) -> bool:
        """Whether this decision may ever reach the ledger, given a human sign-off."""
        return self.tier is not Tier.NEEDS_INFO

    @property
    def disagrees_with_model(self) -> bool:
        """True when a rule decided one account and the model would have picked another.

        Worth surfacing rather than hiding: a rule the model consistently
        disagrees with is either a stale rule or a miscalibrated model, and
        either way somebody should look.
        """
        if not self.second_opinion or not self.account_key:
            return False
        return self.second_opinion.account_key != self.account_key


@dataclass(frozen=True)
class ProviderResult:
    """What an AI provider returns. Deliberately narrow."""

    account_key: str
    confidence: float
    rationale: str
    needs_review: bool = False
    review_reason: str | None = None
    raw_response: str | None = None
    model: str | None = None
