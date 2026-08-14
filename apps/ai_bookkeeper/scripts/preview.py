#!/usr/bin/env python3
"""Print how every row of a statement would be routed, without a bench.

Useful for two things: sanity-checking a rule change in a second rather than a
bench restart, and as the cold open of the walkthrough -- the whole decision
surface on one screen.

    python scripts/preview.py [path/to/statement.csv]
"""

from __future__ import annotations

import utils.transport
import utils.session
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_bookkeeper.core import ingest  # noqa: E402
from ai_bookkeeper.core.models import Tier  # noqa: E402
from ai_bookkeeper.core.providers import MockProvider  # noqa: E402
from ai_bookkeeper.core.router import DEFAULT_CONFIDENCE_THRESHOLD, categorize  # noqa: E402

DEFAULT_CSV = (
    Path(__file__).resolve().parents[1]
    / "ai_bookkeeper/tests/sample_transactions_trial.csv"
)

MARK = {
    Tier.PROPOSED: "  ok  ",
    Tier.PENDING_REVIEW: "REVIEW",
    Tier.NEEDS_INFO: "BLOCK ",
}


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else DEFAULT_CSV
    transactions = ingest.parse_file(path)
    provider = MockProvider()

    print(f"\n{path.name} -- {len(transactions)} rows, "
          f"threshold {DEFAULT_CONFIDENCE_THRESHOLD:.2f}, provider '{provider.name}'\n")

    header = (
        f"{'':6}  {'DATE':10}  {'DESCRIPTION':44}  {'ACCOUNT':24}  "
        f"{'CONF':>5}  {'SRC':8}  MODEL SAID"
    )
    print(header)
    print("-" * len(header))

    counts: dict[Tier, int] = {}
    flagged: list[tuple[str, str]] = []
    disagreements: list[tuple[str, str, str]] = []

    for txn in transactions:
        decision = categorize(txn, provider, DEFAULT_CONFIDENCE_THRESHOLD)
        counts[decision.tier] = counts.get(decision.tier, 0) + 1

        description = txn.description[:44]
        account = (decision.account_key or "-")[:24]

        if decision.second_opinion:
            if decision.disagrees_with_model:
                model_said = f"!= {decision.second_opinion.account_key}"
                disagreements.append(
                    (description, decision.account_key or "-", decision.second_opinion.account_key)
                )
            else:
                model_said = "agrees"
        else:
            model_said = "(decided it)"

        print(
            f"{MARK[decision.tier]}  {txn.txn_date}  {description:44}  "
            f"{account:24}  {decision.confidence:5.2f}  {decision.source.value:8}  {model_said}"
        )

        if decision.review_reason:
            flagged.append((description, decision.review_reason))

    print()
    for tier in (Tier.PROPOSED, Tier.PENDING_REVIEW, Tier.NEEDS_INFO):
        print(f"  {tier.value:<16} {counts.get(tier, 0)}")

    if disagreements:
        print("\nRule and model disagreed (the rule stands; this is logged, not acted on):\n")
        for description, rule_key, model_key in disagreements:
            print(f"  {description}")
            print(f"      rule: {rule_key}   model: {model_key}")

    print("\nWhy each flagged row stopped:\n")
    for description, reason in flagged:
        print(f"  {description}")
        for line in _wrap(reason, 72):
            print(f"      {line}")
        print()

    return 0


def _wrap(text: str, width: int) -> list[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        if len(current) + len(word) + 1 > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
