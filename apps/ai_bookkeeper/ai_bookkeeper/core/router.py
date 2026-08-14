"""Turns a transaction into a routed ``Decision``.

Order of authority, highest first:

1. A deterministic rule. If one fires, the model is never called.
2. The model, constrained to the account vocabulary.
3. The confidence threshold, which can demote a model answer to review.

Note what cannot happen here: nothing in this module posts anything. It decides
which tier a draft lands in. The ledger is only ever touched by a human
submitting that draft.
"""

from __future__ import annotations

from . import rules
from .models import Decision, ProviderResult, RawTransaction, Source, Tier
from .providers.base import PROMPT_VERSION, CategorizationProvider, ProviderError

#: Below this, a model answer goes to a human even if the model didn't ask for
#: one. Overridable per site in AI Bookkeeping Settings -- it is a policy knob
#: for the bookkeeping lead, not a constant for the developer.
DEFAULT_CONFIDENCE_THRESHOLD = 0.70


def categorize(
    txn: RawTransaction,
    provider: CategorizationProvider,
    threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    second_opinion: bool = True,
) -> Decision:
    rule_decision = rules.apply(txn)
    if rule_decision is not None:
        # A rule decided this row. Ask the model anyway, and record what it
        # would have said.
        #
        # Two reasons, neither of which is "let the model overrule the rule":
        # every transaction ends up carrying an AI proposal, and disagreement
        # between a rule and the model becomes visible instead of silent. A
        # rule the model keeps disagreeing with is a signal -- a vendor that
        # changed what it sells, a rule written too broadly, or a model worth
        # re-checking. Silence there is how a stale rule survives for a year.
        #
        # The rule's answer is still the decision. This only annotates it.
        if second_opinion:
            rule_decision.second_opinion = _try_second_opinion(txn, provider)
            if rule_decision.second_opinion:
                rule_decision.model = rule_decision.second_opinion.model
                rule_decision.prompt_version = PROMPT_VERSION
        return rule_decision

    try:
        result = provider.categorize(txn)
    except ProviderError as exc:
        # The model being unavailable must never become a posting decision.
        # The row stops and says why.
        return Decision(
            tier=Tier.PENDING_REVIEW,
            source=Source.FALLBACK,
            account_key=None,
            confidence=0.0,
            rationale=f"AI step unavailable: {exc}",
            review_reason=(
                "No AI proposal available for this transaction "
                f"({exc}). Categorise manually."
            ),
            lines=[],
            model=getattr(provider, "model", None),
            prompt_version=PROMPT_VERSION,
        )

    below_threshold = result.confidence < threshold
    if result.needs_review or below_threshold:
        tier = Tier.PENDING_REVIEW
        if result.review_reason:
            review_reason = result.review_reason
        else:
            review_reason = (
                f"Model confidence {result.confidence:.2f} is below the "
                f"{threshold:.2f} threshold for unattended proposals."
            )
        if below_threshold and result.review_reason:
            review_reason += (
                f" (Confidence {result.confidence:.2f}, threshold {threshold:.2f}.)"
            )
    else:
        tier = Tier.PROPOSED
        review_reason = None

    decision = Decision(
        tier=tier,
        source=Source.LLM,
        account_key=result.account_key,
        confidence=result.confidence,
        rationale=result.rationale,
        review_reason=review_reason,
        lines=rules._two_line(txn, result.account_key, remark="AI proposal"),
        raw_response=result.raw_response,
        model=result.model,
        prompt_version=PROMPT_VERSION,
    )
    rules._annotate_related_party(txn, decision)
    return decision


def _try_second_opinion(
    txn: RawTransaction, provider: CategorizationProvider
) -> ProviderResult | None:
    """Ask the model for a view on a row a rule already decided.

    Failure here is not an error: the decision is already made and does not
    depend on this. A provider outage should cost an annotation, not a row.
    """
    try:
        return provider.categorize(txn)
    except ProviderError:
        return None
