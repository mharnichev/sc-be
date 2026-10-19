from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from app.schemas.common import ORMModel


class Packaging(BaseModel):
    model_config = ConfigDict(extra='forbid')
    weight_kg: float = Field(gt=0, le=1000, allow_inf_nan=False)
    length_cm: int = Field(gt=0, le=1000, strict=True)
    width_cm: int = Field(gt=0, le=1000, strict=True)
    height_cm: int = Field(gt=0, le=1000, strict=True)
    places: int = Field(ge=1, le=100, strict=True)
    payer: Literal['sender', 'recipient']
    declared_value_minor: int = Field(gt=0, le=9999999999, strict=True)
    cod_amount_minor: int = Field(ge=0, le=9999999999, strict=True)


class ShipmentAddress(BaseModel):
    model_config = ConfigDict(extra='forbid')
    shippingCityRef: str = Field(pattern=r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')
    shippingWarehouseRef: str | None = Field(default=None, pattern=r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')
    shippingStreetRef: str | None = Field(default=None, pattern=r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')
    cityLabel: str = Field(min_length=1, max_length=255)
    warehouseLabel: str | None = Field(default=None, max_length=255)
    streetLabel: str | None = Field(default=None, max_length=255)
    buildingNumber: str | None = Field(default=None, max_length=50)
    shippingApartment: str | None = Field(default=None, max_length=50)

    @model_validator(mode='after')
    def destination(self):
        if bool(self.shippingWarehouseRef) == bool(self.shippingStreetRef):
            raise ValueError('Choose exactly one warehouse or street ref')
        if self.shippingWarehouseRef and not self.warehouseLabel:
            raise ValueError('Warehouse label required')
        if self.shippingStreetRef and (not self.streetLabel or not self.buildingNumber):
            raise ValueError('Street label and building number required')
        return self


class ShipmentFixture(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=1, strict=True)
    address_snapshot: ShipmentAddress | None = None


class ShipmentCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=1, strict=True)
    source: Literal['create', 'attach'] = 'create'
    address_snapshot: ShipmentAddress | None = None
    packaging: Packaging
    tracking_number: str | None = Field(default=None, pattern=r'^MOCK-[0-9A-F]{32}$')
    mock_outcome: Literal['accepted', 'timeout_after_acceptance'] = 'accepted'

    @model_validator(mode='after')
    def validate_source(self):
        if (self.source == 'attach') != bool(self.tracking_number):
            raise ValueError('Only attach requires an existing MOCK tracking number')
        if self.source == 'attach' and self.mock_outcome != 'accepted':
            raise ValueError('Timeout fixture is available only for create')
        return self


class ShipmentCommand(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=1, strict=True)
    mock_status: Literal['created', 'in_transit', 'delivered', 'refused', 'returning', 'returned', 'unrecognized'] | None = None
    evidence: str | None = Field(default=None, min_length=1, max_length=500)


class PublicShipmentResponse(ORMModel):
    id: int
    tracking_number: str | None
    status: str
    simulation: bool
    carrier: str
    last_checked_at: datetime | None
    shop_carrier_return_case_id: int | None = None
    provenance: Literal['shop_carrier_return_test'] | None = None
    authority: Literal['test'] | None = None
    return_of_shipment_id: int | None = None


class ShipmentEventResponse(ORMModel):
    operation: str
    raw_status: str
    normalized_status: str
    outcome: str
    created_at: datetime


class ShipmentResponse(PublicShipmentResponse):
    source: str
    active: bool
    operation_uuid: str
    address_snapshot: dict
    packaging: dict
    raw_status: str
    handed_over_at: datetime | None
    events: list[ShipmentEventResponse] = Field(default_factory=list)
    label_url: str | None = None


class GuestTrackingResponse(BaseModel):
    order_id: int
    shipments: list[PublicShipmentResponse]


class MockShipmentFixtureResponse(BaseModel):
    tracking_number: str = Field(pattern=r'^MOCK-[0-9A-F]{32}$')
    simulation: Literal[True]
    revision: int


class TrackingEventResponse(ORMModel):
    raw_status: str
    normalized_status: str
    event_at: datetime | None
    observed_at: datetime


class ShipmentItemResponse(ORMModel):
    order_item_id: int = Field(gt=0, strict=True)
    quantity: int = Field(gt=0, strict=True)


class CustomerShipmentResponse(PublicShipmentResponse):
    delivery_status: str
    provider_status_code: str | None = None
    provider_event_at: datetime | None = None
    last_successful_check_at: datetime | None = None
    stale: bool = True
    last_attempt_status: str = 'never'
    return_of_shipment_id: int | None = None
    items: list[ShipmentItemResponse] | None = None
    events: list[TrackingEventResponse] = Field(default_factory=list)


class RealShipmentAttach(BaseModel):
    """Register an existing waybill; never creates anything at the carrier."""
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=1, strict=True)
    tracking_number: str = Field(pattern=r'^\d{14}$')
    return_of_shipment_id: int | None = Field(default=None, gt=0)
    items: list[ShipmentItemResponse] = Field(default_factory=list, max_length=100)


class TrackingRefreshResponse(BaseModel):
    status: Literal['queued', 'cooldown', 'disabled', 'no_real_shipments']
    retry_after_seconds: int = 0
