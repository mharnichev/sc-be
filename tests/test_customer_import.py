from __future__ import annotations

from decimal import Decimal

from app.utils.import_customers import parse_customer_csv


def test_customer_csv_import_parser_merges_duplicates_and_skips_bad_rows(tmp_path) -> None:
    source = tmp_path / "customers.csv"
    source.write_text(
        "\ufeff,ім'я,Телефон,Витрачено грн.,Дата останнього візиту\n"
        ",Ivan Petrenko,+380 50 111 22 33,3250,23.07.20 13:56\n"
        ",Ivan P,+380501112233,-,Новий клієнт\n"
        ",Bad Phone,---,100,23.07.20 13:56\n",
        encoding="utf-8",
    )

    rows, stats = parse_customer_csv(source)

    assert stats.rows_read == 3
    assert stats.valid_rows == 2
    assert stats.invalid_phone_rows == [4]
    assert stats.duplicate_rows_merged == 1
    assert len(rows) == 1
    assert rows[0].phone == "+380501112233"
    assert rows[0].full_name == "Ivan Petrenko"
    assert rows[0].total_spent == Decimal("3250.00")
    assert rows[0].last_visit_at is not None
    assert rows[0].is_new_client is False
