"""Pure boundary regressions for B3: callers cannot manufacture carrier truth."""
import ast
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from app.core.config import Settings
from app.schemas.shop_carrier_returns import CarrierCreate, CarrierReceive, CarrierClose
from app.schemas.shop_fiscal import ReturnCommand

CREATE = dict(expected_revision=1, original_shipment_id=2,
    lines=[dict(order_item_id=3, quantity=1)], evidence="Synthetic local test", confirm=True)
RECEIVE = dict(expected_revision=1, lines=[dict(order_item_id=3, quantity=1, disposition="restock")],
    evidence="Physically checked", confirm=True)
CLOSE = dict(expected_revision=1, evidence="Unpaid full refusal checked", confirm=True)


@pytest.mark.parametrize("field,value", [
    ("status", "returned"), ("authority", "production"), ("simulation", False),
    ("tracking_number", "59000000000001"), ("provider_uuid", "external"),
    ("amount_minor", 10000), ("financial_mode", "unpaid_cod"),
])
def test_create_cannot_accept_client_supplied_carrier_or_financial_truth(field, value):
    with pytest.raises(ValidationError):
        CarrierCreate(**{**CREATE, field: value})


@pytest.mark.parametrize("model,body", [(CarrierCreate, CREATE), (CarrierReceive, RECEIVE), (CarrierClose, CLOSE)])
@pytest.mark.parametrize("confirmation", [False, 1, "true", None])
def test_consequential_carrier_commands_require_literal_confirmation(model, body, confirmation):
    with pytest.raises(ValidationError):
        model(**{**body, "confirm": confirmation})


@pytest.mark.parametrize("quantity", [True, "1", 1.1, 0, -1])
def test_receipt_cannot_coerce_return_quantity(quantity):
    with pytest.raises(ValidationError):
        CarrierReceive(**{**RECEIVE, "lines": [{**RECEIVE["lines"][0], "quantity": quantity}]})


@pytest.mark.parametrize("receipt_id", [True, "1", 1.1, 0, -1])
def test_financial_claim_requires_strict_positive_receipt_identity(receipt_id):
    with pytest.raises(ValidationError):
        ReturnCommand(expected_revision=1, sale_document_id=2,
            carrier_receipt_id=receipt_id, lines=[dict(order_item_id=3, quantity=1)], reason="Synthetic")


def test_carrier_return_flag_defaults_closed_without_dotenv():
    assert Settings(_env_file=None).shop_carrier_returns_mock_enabled is False


def test_0124_raw_sql_does_not_accidentally_declare_alembic_bind_parameters():
    # A ':shipment' literal previously became an unbound SQLAlchemy parameter,
    # breaking upgrade before any business fact could be created.
    source = Path(__file__).resolve().parents[1] / "alembic/versions/0124_shop_carrier_returns.py"
    statements = [node.args[0].value for node in ast.walk(ast.parse(source.read_text()))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == "execute" and node.args and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)]
    assert statements
    for statement in statements:
        assert not text(statement)._bindparams, statement
