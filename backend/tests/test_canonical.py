from decimal import Decimal

import numpy as np
import pytest

from app.core.canonical import MISSING_MARKERS, RawRow, to_canonical


def test_formatting_noise_is_equal():
    a, b, c = (to_canonical(RawRow("d", "i", "FRA", "2024", v)) for v in ("1.50", 1.5, 1.5000000001))
    assert a.value_hash == b.value_hash == c.value_hash


@pytest.mark.parametrize(
    "marker",
    sorted(MISSING_MARKERS)
    + [m.upper() for m in sorted(MISSING_MARKERS)]
    + [f"  {m}  " for m in sorted(MISSING_MARKERS)],
)
def test_missing_markers_are_absent(marker):
    assert to_canonical(RawRow("d", "i", "FRA", "2024", marker)) is None


@pytest.mark.parametrize("v", [float("nan"), None, "NaN"])
def test_nan_and_none_are_absent(v):
    assert to_canonical(RawRow("d", "i", "FRA", "2024", v)) is None


def test_text_values_kept():
    r = to_canonical(RawRow("d", "status", "FRA", "2024", " Free "))
    assert r.value_text == "Free" and r.value_num is None


def test_dims_order_does_not_change_key():
    a = to_canonical(RawRow("d", "i", "FRA", "2024", 1, {"sex": "F", "age": "15-24"}))
    b = to_canonical(RawRow("d", "i", "FRA", "2024", 1, {"age": "15-24", "sex": "F"}))
    assert a.key_hash == b.key_hash


def test_thousands_separator_and_negative():
    assert to_canonical(RawRow("d", "i", "FRA", "2024", "-1,234.5")).value_num == Decimal("-1234.5")


def test_default_dims_is_empty_and_each_row_gets_its_own():
    a = RawRow("d", "i", "FRA", "2024", 1)
    b = RawRow("d", "i", "FRA", "2024", 1)
    assert dict(a.dims) == {}
    assert a.dims is not b.dims


def test_bool_is_rejected():
    with pytest.raises(TypeError):
        to_canonical(RawRow("d", "i", "FRA", "2024", True))


@pytest.mark.parametrize("v", [float("inf"), float("-inf"), "inf", "-Infinity"])
def test_infinite_values_are_missing(v):
    assert to_canonical(RawRow("d", "i", "FRA", "2024", v)) is None


def test_rounds_to_ten_significant_digits():
    r = to_canonical(RawRow("d", "i", "FRA", "2024", "1.23456789012345"))
    assert r.value_num == Decimal("1.23456789")


def test_integer_forms_hash_identically():
    hashes = {
        to_canonical(RawRow("d", "i", "FRA", "2024", v)).value_hash
        for v in (100, 100.0, "100", "1E+2", Decimal("100"), Decimal("1E+2"), np.int64(100), np.float64(100.0))
    }
    assert len(hashes) == 1


def test_negative_zero_equals_zero():
    a = to_canonical(RawRow("d", "i", "FRA", "2024", "-0"))
    b = to_canonical(RawRow("d", "i", "FRA", "2024", 0))
    assert a.value_hash == b.value_hash


def test_numpy_numbers_are_accepted():
    r = to_canonical(RawRow("d", "i", "FRA", "2024", np.float32(1.1)))
    assert r.value_num == Decimal("1.1")


@pytest.mark.parametrize("v", ["1 2", "1_000"])
def test_non_numeric_spacing_or_underscores_stay_text(v):
    r = to_canonical(RawRow("d", "i", "FRA", "2024", v))
    assert r.value_num is None and r.value_text == v


def test_unsupported_type_is_rejected():
    with pytest.raises(TypeError):
        to_canonical(RawRow("d", "i", "FRA", "2024", ["1"]))


@pytest.mark.parametrize("v", ["1,5", "1,23", "12,34,567", "1,2345"])
def test_comma_not_in_thousands_grouping_stays_text(v):
    r = to_canonical(RawRow("d", "i", "FRA", "2024", v))
    assert r.value_num is None and r.value_text == v


@pytest.mark.parametrize("v", ["1,234", "-1,234,567.25", "+12,345"])
def test_well_formed_thousands_grouping_is_a_number(v):
    r = to_canonical(RawRow("d", "i", "FRA", "2024", v))
    assert r.value_text is None and r.value_num is not None


@pytest.mark.parametrize("v", ["\u0661\u0662\u0663", "\u0661,\u0662\u0663\u0664", "\uff11\uff12"])
def test_non_ascii_digits_stay_text(v):
    r = to_canonical(RawRow("d", "i", "FRA", "2024", v))
    assert r.value_num is None and r.value_text == v


@pytest.mark.parametrize("v", ["1E1000000", "-1E1000000"])
def test_decimal_overflow_is_kept_as_text(v):
    r = to_canonical(RawRow("d", "i", "FRA", "2024", v))
    assert r.value_num is None and r.value_text == v
