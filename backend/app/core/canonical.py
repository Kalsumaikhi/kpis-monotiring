"""Canonical rows and value normalisation.

Raw source values are turned into canonical rows with stable key and value
hashes, so formatting noise (trailing zeros, thousands separators, float
representation) never looks like a change.
"""

from __future__ import annotations

import hashlib
import json
import math
import numbers
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Mapping

MISSING_MARKERS = {"", "..", "...", "n/a", "na", "nan", "-", "—", "none", "null"}

_SIGNIFICANT_DIGITS = 10

# ASCII digits only ([0-9], not \d), so non-Latin digit characters stay text.
_PLAIN_NUMBER = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_GROUPED_NUMBER = re.compile(r"[+-]?[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?")
_INFINITE_WORDS = {"inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"}


@dataclass(frozen=True)
class RawRow:
    dataset: str
    indicator: str
    country: str
    period: str
    value: Any
    dims: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CanonicalRow:
    dataset: str
    indicator: str
    country: str
    period: str
    dims_json: str
    key_hash: str
    value_num: Decimal | None
    value_text: str | None
    value_hash: str


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _normalise_decimal(d: Decimal) -> Decimal | None:
    """Round to 10 significant digits and strip trailing zeros.

    Returns None for NaN or infinity. Negative zero becomes plain zero.
    """
    if not d.is_finite():
        return None
    with localcontext() as ctx:
        ctx.prec = _SIGNIFICANT_DIGITS
        rounded = +d  # unary plus applies the context precision
    normalised = rounded.normalize()
    if normalised == 0:
        return Decimal(0)
    return normalised


def _number_to_decimal(v: Any) -> Decimal | None:
    if isinstance(v, Decimal):
        return v
    if isinstance(v, numbers.Integral):
        return Decimal(int(v))
    # float and other real numbers (including numpy scalars). str() keeps the
    # shortest representation, so np.float32(1.1) becomes 1.1, not 1.10000002.
    if not math.isfinite(float(v)):
        return None
    try:
        return Decimal(str(v))
    except InvalidOperation:
        # e.g. fractions.Fraction, whose str() is "1/2"
        return Decimal(repr(float(v)))


def _text_to_value(s: str) -> tuple[Decimal | None, str | None] | None:
    trimmed = s.strip()
    if trimmed.lower() in MISSING_MARKERS or trimmed.lower() in _INFINITE_WORDS:
        return None
    if "," in trimmed:
        # Commas only count as thousands separators in a well-formed grouping ("1,234.5").
        if not _GROUPED_NUMBER.fullmatch(trimmed):
            return (None, trimmed)
        candidate = trimmed.replace(",", "")
    elif _PLAIN_NUMBER.fullmatch(trimmed):
        candidate = trimmed
    else:
        return (None, trimmed)
    try:
        num = _normalise_decimal(Decimal(candidate))
    except ArithmeticError:
        # e.g. "1E1000000" overflows the decimal context; keep it as text.
        return (None, trimmed)
    if num is None:
        return None
    return (num, None)


def canonicalize_value(v: Any) -> tuple[Decimal | None, str | None] | None:
    """Return (value_num, value_text), or None when the value is missing.

    Raises TypeError for bools and for types that are neither text nor numbers.
    """
    if v is None:
        return None
    if isinstance(v, bool):
        raise TypeError("bool is not a valid KPI value")
    if isinstance(v, str):
        return _text_to_value(v)
    if isinstance(v, (numbers.Real, Decimal)):
        d = _number_to_decimal(v)
        if d is None:
            return None
        num = _normalise_decimal(d)
        if num is None:
            return None
        return (num, None)
    raise TypeError(f"unsupported value type: {type(v).__name__}")


def to_canonical(row: RawRow) -> CanonicalRow | None:
    parsed = canonicalize_value(row.value)
    if parsed is None:
        return None
    value_num, value_text = parsed

    dims_json = json.dumps(dict(row.dims), sort_keys=True, separators=(",", ":"))
    key_hash = _sha1(
        "\x1f".join([row.dataset, row.indicator, row.country, row.period, dims_json])
    )
    if value_num is not None:
        value_hash = _sha1("n:" + format(value_num, "f"))
    else:
        value_hash = _sha1("t:" + value_text)

    return CanonicalRow(
        dataset=row.dataset,
        indicator=row.indicator,
        country=row.country,
        period=row.period,
        dims_json=dims_json,
        key_hash=key_hash,
        value_num=value_num,
        value_text=value_text,
        value_hash=value_hash,
    )
