from __future__ import annotations

import argparse
import csv
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.models.customer import Customer

KYIV_TZ = ZoneInfo("Europe/Kyiv")
PHONE_ALLOWED_CHARS = set("+0123456789() -")
CSV_NAME_FIELD = "ім'я"
CSV_PHONE_FIELD = "Телефон"
CSV_SPENT_FIELD = "Витрачено грн."
CSV_LAST_VISIT_FIELD = "Дата останнього візиту"
NEW_CLIENT_VALUES = {"новий клієнт", "новий клиент", "новый клиент", "new customer"}
LAST_VISIT_FORMATS = (
    "%d.%m.%y %H:%M",
    "%d.%m.%Y %H:%M",
    "%d.%m.%y %H:%M:%S",
    "%d.%m.%Y %H:%M:%S",
    "%d.%m.%Y",
    "%d.%m.%y",
)


@dataclass
class CustomerImportRow:
    row_number: int
    phone: str
    full_name: str | None
    total_spent: Decimal
    last_visit_at: datetime | None
    is_new_client: bool


@dataclass
class CustomerImportStats:
    rows_read: int = 0
    header_rows_skipped: int = 0
    invalid_phone_rows: list[int] = field(default_factory=list)
    invalid_last_visit_rows: list[int] = field(default_factory=list)
    duplicate_rows_merged: int = 0
    customers_created: int = 0
    customers_updated: int = 0
    customers_unchanged: int = 0

    @property
    def valid_rows(self) -> int:
        return self.rows_read - self.header_rows_skipped - len(self.invalid_phone_rows) - len(self.invalid_last_visit_rows)


def normalize_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


def normalize_phone(phone: str | None) -> str | None:
    if phone is None:
        return None
    normalized = "".join(char for char in phone.strip() if char in PHONE_ALLOWED_CHARS)
    normalized = normalized.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if normalized and not normalized.startswith("+"):
        normalized = f"+{normalized}"
    digits = normalized.removeprefix("+")
    if not digits.isdigit() or len(digits) < 10 or len(digits) > 15:
        return None
    return normalized


def split_customer_name(full_name: str | None) -> tuple[str | None, str | None]:
    if not full_name:
        return None, None
    parts = full_name.strip().split(maxsplit=1)
    if not parts:
        return None, None
    return parts[0][:100], parts[1][:100] if len(parts) > 1 else None


def parse_money(value: str | None) -> Decimal:
    text = normalize_text(value)
    if text is None or text.casefold() in NEW_CLIENT_VALUES or text == "-":
        return Decimal("0.00")
    normalized = text.replace("\xa0", "").replace(" ", "").replace(",", ".")
    match = re.search(r"-?\d+(?:\.\d+)?", normalized)
    if not match:
        return Decimal("0.00")
    try:
        amount = Decimal(match.group())
    except InvalidOperation:
        return Decimal("0.00")
    return max(amount, Decimal("0.00")).quantize(Decimal("0.01"))


def parse_last_visit(value: str | None) -> tuple[datetime | None, bool, bool]:
    text = normalize_text(value)
    if text is None:
        return None, False, False
    if text.casefold() in NEW_CLIENT_VALUES:
        return None, True, False
    for date_format in LAST_VISIT_FORMATS:
        try:
            parsed = datetime.strptime(text, date_format)
            return parsed.replace(tzinfo=KYIV_TZ), False, False
        except ValueError:
            pass
    return None, False, True


def merge_rows(existing: CustomerImportRow, incoming: CustomerImportRow) -> CustomerImportRow:
    if incoming.last_visit_at and (existing.last_visit_at is None or incoming.last_visit_at > existing.last_visit_at):
        existing.last_visit_at = incoming.last_visit_at
    if incoming.total_spent > existing.total_spent:
        existing.total_spent = incoming.total_spent
    if not existing.full_name and incoming.full_name:
        existing.full_name = incoming.full_name
    existing.is_new_client = existing.last_visit_at is None and (existing.is_new_client or incoming.is_new_client)
    return existing


def parse_customer_csv(file_path: Path) -> tuple[list[CustomerImportRow], CustomerImportStats]:
    stats = CustomerImportStats()
    by_phone: dict[str, CustomerImportRow] = {}

    with file_path.open(encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        for row_number, row in enumerate(reader, start=2):
            stats.rows_read += 1
            if normalize_text(row.get(CSV_PHONE_FIELD)) == CSV_PHONE_FIELD:
                stats.header_rows_skipped += 1
                continue

            phone = normalize_phone(row.get(CSV_PHONE_FIELD))
            if phone is None:
                stats.invalid_phone_rows.append(row_number)
                continue

            last_visit_at, is_new_client, invalid_last_visit = parse_last_visit(row.get(CSV_LAST_VISIT_FIELD))
            if invalid_last_visit:
                stats.invalid_last_visit_rows.append(row_number)
                continue

            parsed_row = CustomerImportRow(
                row_number=row_number,
                phone=phone,
                full_name=normalize_text(row.get(CSV_NAME_FIELD)),
                total_spent=parse_money(row.get(CSV_SPENT_FIELD)),
                last_visit_at=last_visit_at,
                is_new_client=is_new_client,
            )
            if phone in by_phone:
                by_phone[phone] = merge_rows(by_phone[phone], parsed_row)
                stats.duplicate_rows_merged += 1
            else:
                by_phone[phone] = parsed_row

    return list(by_phone.values()), stats


def build_customer_payload(row: CustomerImportRow, *, overwrite_names: bool) -> dict:
    name, surname = split_customer_name(row.full_name)
    payload = {
        "imported_total_spent": row.total_spent,
        "imported_last_visit_at": row.last_visit_at,
        "imported_is_new_client": row.is_new_client,
    }
    if overwrite_names:
        payload["name"] = name
        payload["surname"] = surname
    else:
        payload["name_if_empty"] = name
        payload["surname_if_empty"] = surname
    return payload


def upsert_customers(
    session: Session,
    rows: list[CustomerImportRow],
    stats: CustomerImportStats,
    *,
    overwrite_names: bool = False,
) -> None:
    for row in rows:
        customer = session.execute(select(Customer).where(Customer.phone == row.phone)).scalar_one_or_none()
        payload = build_customer_payload(row, overwrite_names=overwrite_names)

        if customer is None:
            customer = Customer(
                phone=row.phone,
                name=payload.pop("name_if_empty", None) or payload.pop("name", None),
                surname=payload.pop("surname_if_empty", None) or payload.pop("surname", None),
                is_active=True,
                **payload,
            )
            session.add(customer)
            stats.customers_created += 1
            continue

        changed = False
        name_if_empty = payload.pop("name_if_empty", None)
        surname_if_empty = payload.pop("surname_if_empty", None)
        if name_if_empty and not customer.name:
            customer.name = name_if_empty
            changed = True
        if surname_if_empty and not customer.surname:
            customer.surname = surname_if_empty
            changed = True

        for key, value in payload.items():
            if getattr(customer, key) != value:
                setattr(customer, key, value)
                changed = True

        if changed:
            stats.customers_updated += 1
        else:
            stats.customers_unchanged += 1


def import_customers(
    file_path: Path,
    *,
    dry_run: bool = False,
    overwrite_names: bool = False,
) -> CustomerImportStats:
    rows, stats = parse_customer_csv(file_path)
    if dry_run:
        return stats

    engine = create_engine(settings.sqlalchemy_sync_database_uri, future=True)
    SessionLocal = sessionmaker(bind=engine, future=True)
    with SessionLocal() as session:
        upsert_customers(session, rows, stats, overwrite_names=overwrite_names)
        session.commit()
    return stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import customers from CSV")
    parser.add_argument("--file", required=True, help="Path to CSV file")
    parser.add_argument("--dry-run", action="store_true", help="Validate and summarize without writing to the database")
    parser.add_argument(
        "--overwrite-names",
        action="store_true",
        help="Overwrite existing customer names with names from the CSV",
    )
    return parser.parse_args()


def format_stats(stats: CustomerImportStats) -> str:
    return " ".join(
        [
            "Import completed:",
            f"rows_read={stats.rows_read}",
            f"header_rows_skipped={stats.header_rows_skipped}",
            f"valid_rows={stats.valid_rows}",
            f"invalid_phone_rows={len(stats.invalid_phone_rows)}",
            f"invalid_last_visit_rows={len(stats.invalid_last_visit_rows)}",
            f"duplicate_rows_merged={stats.duplicate_rows_merged}",
            f"customers_created={stats.customers_created}",
            f"customers_updated={stats.customers_updated}",
            f"customers_unchanged={stats.customers_unchanged}",
        ]
    )


if __name__ == "__main__":
    args = parse_args()
    result = import_customers(
        Path(args.file),
        dry_run=args.dry_run,
        overwrite_names=args.overwrite_names,
    )
    print(format_stats(result))
    if result.invalid_phone_rows:
        print("Invalid phone row numbers:", ",".join(str(item) for item in result.invalid_phone_rows[:50]))
    if result.invalid_last_visit_rows:
        print("Invalid last visit row numbers:", ",".join(str(item) for item in result.invalid_last_visit_rows[:50]))
