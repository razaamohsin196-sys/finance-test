"""CSV parsing, kept separate from the Frappe importer so it can be tested alone.

The sample file carries an ``account_hint`` column that is empty on every row.
That is almost certainly deliberate -- there is no shortcut to lean on -- but a
parser that assumes the column is either always present or always populated
breaks on the first real bank export. Both cases are handled and the hint, when
present, is advisory only: it never overrides a rule.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from io import StringIO
from pathlib import Path

from .models import Direction, RawTransaction

REQUIRED_COLUMNS = ("date", "description", "amount", "currency", "direction")

_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%b-%Y")


class IngestError(ValueError):
    """Raised for a row or file that cannot be parsed."""


def _parse_date(value: str) -> date:
    text = (value or "").strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise IngestError(f"Unrecognised date {value!r}")


def _parse_amount(value: str) -> Decimal:
    text = (value or "").strip().replace(",", "").replace("$", "")
    # Accounting-style negatives: (350.00) means -350.00
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    if not text:
        raise IngestError("Empty amount")
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise IngestError(f"Unrecognised amount {value!r}") from exc


def _parse_direction(value: str) -> Direction:
    text = (value or "").strip().lower()
    if text in {"debit", "dr", "withdrawal", "out"}:
        return Direction.DEBIT
    if text in {"credit", "cr", "deposit", "in"}:
        return Direction.CREDIT
    raise IngestError(f"Unrecognised direction {value!r}")


def parse_rows(handle) -> Iterator[tuple[int, RawTransaction | None, str | None]]:
    """Yield ``(line_number, transaction, error)`` for every data row.

    Bad rows yield an error rather than aborting the file: a single malformed
    line in a bank export should not block the other fourteen from reaching a
    reviewer.
    """
    reader = csv.DictReader(handle)
    if reader.fieldnames is None:
        raise IngestError("File is empty")

    headers = {(name or "").strip().lower() for name in reader.fieldnames}
    missing = [c for c in REQUIRED_COLUMNS if c not in headers]
    if missing:
        raise IngestError(f"Missing required column(s): {', '.join(missing)}")

    for offset, row in enumerate(reader, start=2):
        clean = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        if not any(clean.get(c) for c in REQUIRED_COLUMNS):
            continue  # blank trailing line
        try:
            txn = RawTransaction(
                txn_date=_parse_date(clean.get("date", "")),
                description=clean.get("description", ""),
                amount=_parse_amount(clean.get("amount", "")),
                currency=(clean.get("currency") or "USD").upper(),
                direction=_parse_direction(clean.get("direction", "")),
                # Column may be absent entirely, or present and blank.
                account_hint=clean.get("account_hint") or None,
            )
        except IngestError as exc:
            yield offset, None, str(exc)
        else:
            yield offset, txn, None


def parse_file(path: str | Path) -> list[RawTransaction]:
    """Convenience wrapper. Raises on the first bad row."""
    with open(path, newline="", encoding="utf-8-sig") as handle:
        out = []
        for line_no, txn, error in parse_rows(handle):
            if error:
                raise IngestError(f"line {line_no}: {error}")
            out.append(txn)
        return out


def parse_text(text: str) -> list[RawTransaction]:
    return [t for _, t, err in parse_rows(StringIO(text)) if not err and t]
