"""Offline provider used by the test suite and by anyone without an API key.

This is not a stub that returns a constant. It reproduces the *shape* of real
model behaviour that the router has to cope with:

* a confident answer for a description that clearly names a known category,
* a low-confidence answer with ``needs_review`` set for a genuinely vague one,
* deterministic output, so a test asserting a routing tier does not flake.

That makes the whole pipeline -- import, rules, AI step, routing, draft
creation, audit log -- runnable and testable with no key and no network, which
is the difference between a demo that only works on the author's laptop and one
a reviewer can run.
"""

from __future__ import annotations

import re

from ..models import ProviderResult, RawTransaction
from .base import CategorizationProvider

#: (pattern, account key, confidence, rationale). Ordered; first match wins.
_HEURISTICS: tuple[tuple[str, str, float, str], ...] = (
    (
        r"\bAWS\b|CLOUD|HOSTING",
        "CLOUD_HOSTING",
        0.93,
        "Description names a cloud infrastructure provider.",
    ),
    (
        r"SUBSCRIPTION|\bSAAS\b|MONTHLY",
        "SOFTWARE_SUBSCRIPTIONS",
        0.88,
        "Recurring software subscription.",
    ),
    (
        r"CONTRACT|FREELANCE|UPWORK",
        "CONTRACTOR_FEES",
        0.9,
        "Payment to a contractor.",
    ),
    (
        r"OFFICE|SUPPLIES|DEPOT",
        "OFFICE_SUPPLIES",
        0.87,
        "Office consumables retailer.",
    ),
    (
        r"BANK\s+(SERVICE\s+)?FEE|MAINTENANCE\s+FEE",
        "BANK_CHARGES",
        0.94,
        "Fee charged by the bank.",
    ),
)

#: Marketplace and generic-retail strings: a real merchant, but the description
#: says nothing about what was bought, so the category is genuinely unknown.
_AMBIGUOUS_RETAIL = re.compile(r"AMZN|AMAZON\s+MKTP|WALMART|TARGET|\bMISC\b|\bPOS\b", re.IGNORECASE)


class MockProvider(CategorizationProvider):
    name = "mock"
    model = "mock-deterministic-v1"

    def categorize(self, txn: RawTransaction) -> ProviderResult:
        description = txn.description

        if _AMBIGUOUS_RETAIL.search(description):
            return self._coerce(
                {
                    "account_key": "OFFICE_SUPPLIES",
                    "confidence": 0.34,
                    "rationale": (
                        "General marketplace purchase. The line names the "
                        "marketplace but not the goods, so the category is a "
                        "guess -- office supplies is only the most common case."
                    ),
                    "needs_review": True,
                    "review_reason": (
                        "Marketplace purchase with no indication of what was "
                        "bought. Could be supplies, equipment, software, or a "
                        "personal charge. Needs the receipt."
                    ),
                }
            )

        for pattern, key, confidence, rationale in _HEURISTICS:
            if re.search(pattern, description, re.IGNORECASE):
                return self._coerce(
                    {
                        "account_key": key,
                        "confidence": confidence,
                        "rationale": rationale,
                        "needs_review": False,
                        "review_reason": None,
                    }
                )

        return self._coerce(
            {
                "account_key": "SUSPENSE",
                "confidence": 0.15,
                "rationale": "No recognisable vendor or category in the description.",
                "needs_review": True,
                "review_reason": "Description does not identify a category.",
            }
        )
