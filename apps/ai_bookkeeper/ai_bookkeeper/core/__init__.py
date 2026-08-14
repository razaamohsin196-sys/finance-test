"""Pure categorisation core -- no frappe imports anywhere below this package.

Keeping the accounting judgement out of the ORM layer is what makes the 15-row
fixture runnable as an ordinary pytest suite, with no bench, no database and no
network. See ``ai_bookkeeper/tests``.
"""

from __future__ import annotations

from .models import (
    Decision,
    Direction,
    JournalLine,
    ProviderResult,
    RawTransaction,
    Source,
    Tier,
)
from .router import DEFAULT_CONFIDENCE_THRESHOLD, categorize

__all__ = [
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "Decision",
    "Direction",
    "JournalLine",
    "ProviderResult",
    "RawTransaction",
    "Source",
    "Tier",
    "categorize",
]
