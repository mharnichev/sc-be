from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from app.schemas.product import ShopProductResponse


class ReminderItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: int = Field(gt=0, strict=True)
    quantity: int = Field(ge=1, le=999, strict=True)


class CheckoutReminderCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr = Field(max_length=255)
    locale: Literal["uk", "en"]
    items: list[ReminderItem] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_products(self):
        if len({item.product_id for item in self.items}) != len(self.items):
            raise ValueError("Duplicate checkout products")
        return self


class CheckoutReminderRecovery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=43, max_length=43, pattern=r"^[A-Za-z0-9_-]+$")


class RecoveredReminderItem(BaseModel):
    variant: str = ""
    product_id: int
    quantity: int
    product: ShopProductResponse


class CheckoutReminderRecoveryResponse(BaseModel):
    locale: Literal["uk", "en"]
    checkout_key: str
    items: list[RecoveredReminderItem]
    changes: list[str]
