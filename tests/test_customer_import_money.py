from __future__ import annotations

import csv
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.utils.import_customers import format_stats, parse_customer_csv, parse_money


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "0.00"),
        ("", "0.00"),
        ("   ", "0.00"),
        ("-", "0.00"),
        ("Новий клієнт", "0.00"),
        ("new customer", "0.00"),
        ("not a number", "0.00"),
        ("-12.34", "0.00"),
        pytest.param("-" + "9" * 1000, "0.00", id="huge-negative-clamped"),
        ("1 234,56 грн", "1234.56"),
        ("1\u00a0234,565", "1234.56"),
        ("1234.575", "1234.58"),
        ("99999999.99", "99999999.99"),
        ("99999999.994", "99999999.99"),
    ],
)
def test_parse_money_preserves_valid_and_zero_defaults(value, expected) -> None:
    assert parse_money(value) == Decimal(expected)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("100000000", id="above-maximum"),
        pytest.param("99999999.995", id="rounding-over-maximum"),
        pytest.param("9" * 1000, id="huge-positive"),
    ],
)
def test_parse_money_rejects_numeric_overflow_without_decimal_exception(value) -> None:
    with pytest.raises(ValueError):
        parse_money(value)


def test_customer_csv_skips_invalid_money_before_dedup_with_exact_row_numbers(tmp_path) -> None:
    source = tmp_path / "synthetic-customers.csv"
    with source.open("w", encoding="utf-8-sig", newline="") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(["ім'я", "Телефон", "Витрачено грн.", "Дата останнього візиту"])
        writer.writerows(
            [
                ["ім'я", "Телефон", "Витрачено грн.", "Дата останнього візиту"],
                ["Alice First", "+380501112233", "99999999.99", "20.07.2020"],
                ["Rejected Rename", "+380501112233", "100000000", "30.07.2020"],
                ["Rounding Overflow", "+380501112234", "99999999.995", ""],
                ["Huge Positive", "+380501112235", "9" * 1000, ""],
                ["Huge Negative", "+380501112236", "-" + "9" * 1000, "Новий клієнт"],
                ["Nonnumeric", "+380501112237", "not a number", ""],
                ["Alice Later", "+380 50 111 22 33", "3250", "21.07.2020"],
                ["Invalid Phone", "---", "100000000", ""],
                ["Invalid Date", "+380501112238", "100000000", "not a date"],
                ["Blank Money", "+380501112239", "", "new customer"],
                ["Rounded Money", "+380501112240", "1 234,565", ""],
            ]
        )

    rows, stats = parse_customer_csv(source)

    assert stats.rows_read == 12
    assert stats.header_rows_skipped == 1
    assert stats.invalid_phone_rows == [10]
    assert stats.invalid_last_visit_rows == [11]
    assert stats.invalid_money_rows == [4, 5, 6]
    assert stats.valid_rows == 6
    assert stats.duplicate_rows_merged == 1
    assert stats.customers_created == stats.customers_updated == stats.customers_unchanged == 0
    assert "invalid_money_rows=3" in format_stats(stats)
    assert len(rows) == 5

    by_phone = {row.phone: row for row in rows}
    alice = by_phone["+380501112233"]
    assert alice.row_number == 3
    assert alice.full_name == "Alice First"
    assert alice.total_spent == Decimal("99999999.99")
    assert alice.last_visit_at == datetime(2020, 7, 21, tzinfo=ZoneInfo("Europe/Kyiv"))
    assert alice.is_new_client is False
    assert by_phone["+380501112236"].total_spent == Decimal("0.00")
    assert by_phone["+380501112236"].is_new_client is True
    assert by_phone["+380501112237"].total_spent == Decimal("0.00")
    assert by_phone["+380501112239"].total_spent == Decimal("0.00")
    assert by_phone["+380501112239"].is_new_client is True
    assert by_phone["+380501112240"].total_spent == Decimal("1234.56")
