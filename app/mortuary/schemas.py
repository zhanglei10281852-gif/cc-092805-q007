from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class ReservationKind(str, Enum):
    farewell_hall = "farewell_hall"
    cremator = "cremator"
    cold_storage = "cold_storage"
    vehicle = "vehicle"
    burial_team = "burial_team"


class CaseCreate(BaseModel):
    external_ref: str = Field(min_length=3, max_length=80)
    decedent_name: str = Field(min_length=1, max_length=120)
    identity_number: str | None = Field(default=None, max_length=80)
    death_time: datetime
    received_from: str = Field(min_length=2, max_length=160)
    family_contact: str = Field(min_length=2, max_length=120)
    family_phone: str = Field(min_length=5, max_length=40)
    special_notes: str = Field(default="", max_length=2000)


class CustodyTransferCreate(BaseModel):
    from_location: str = Field(min_length=2, max_length=120)
    to_location: str = Field(min_length=2, max_length=120)
    seal_code: str = Field(min_length=4, max_length=80)
    requested_by: str = Field(min_length=2, max_length=80)
    idempotency_key: str = Field(min_length=8, max_length=120)

    @model_validator(mode="after")
    def locations_must_differ(self):
        if self.from_location == self.to_location:
            raise ValueError("交接起点与终点不能相同")
        return self


class CustodyAccept(BaseModel):
    accepted_by: str = Field(min_length=2, max_length=80)
    observed_seal_code: str = Field(min_length=4, max_length=80)
    condition_note: str = Field(default="", max_length=1000)


class ResourceCreate(BaseModel):
    code: str = Field(min_length=2, max_length=40, pattern=r"^[A-Z0-9_-]+$")
    name: str = Field(min_length=2, max_length=120)
    kind: ReservationKind
    site_code: str = Field(min_length=2, max_length=40)
    capacity: int = Field(default=1, ge=1, le=500)
    attributes: dict[str, Any] = Field(default_factory=dict)


class ReservationCreate(BaseModel):
    resource_code: str = Field(min_length=2, max_length=40)
    case_id: int = Field(gt=0)
    start_at: datetime
    end_at: datetime
    purpose: str = Field(min_length=2, max_length=300)
    created_by: str = Field(min_length=2, max_length=80)
    idempotency_key: str = Field(min_length=8, max_length=120)

    @model_validator(mode="after")
    def validate_interval(self):
        if self.end_at <= self.start_at:
            raise ValueError("结束时间必须晚于开始时间")
        if (self.end_at - self.start_at).total_seconds() > 259200:
            raise ValueError("单次预约不能超过七十二小时")
        return self


class ServiceOrderCreate(BaseModel):
    case_id: int = Field(gt=0)
    service_code: str = Field(min_length=2, max_length=60)
    quantity: int = Field(default=1, ge=1, le=100)
    unit_price_cents: int = Field(ge=0, le=100_000_000)
    requested_by: str = Field(min_length=2, max_length=80)
    notes: str = Field(default="", max_length=1000)


class BurialRightCreate(BaseModel):
    plot_code: str = Field(min_length=2, max_length=80)
    holder_name: str = Field(min_length=2, max_length=120)
    holder_identity: str = Field(min_length=4, max_length=80)
    starts_on: date
    expires_on: date
    case_id: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_period(self):
        if self.expires_on <= self.starts_on:
            raise ValueError("权属到期日必须晚于起始日")
        return self


class BurialRightRenew(BaseModel):
    years: int = Field(ge=1, le=20)
    handled_by: str = Field(min_length=2, max_length=80)
    payment_reference: str = Field(min_length=4, max_length=120)


class InvoiceCreate(BaseModel):
    case_id: int = Field(gt=0)
    order_ids: list[int] = Field(min_length=1, max_length=100)
    created_by: str = Field(min_length=2, max_length=80)


class PaymentCreate(BaseModel):
    amount_cents: int = Field(gt=0, le=100_000_000)
    channel: str = Field(min_length=2, max_length=40)
    external_reference: str = Field(min_length=4, max_length=120)
    received_by: str = Field(min_length=2, max_length=80)


class TransportLegPlan(BaseModel):
    from_station: str = Field(min_length=2, max_length=120)
    to_station: str = Field(min_length=2, max_length=120)
    vehicle_code: str = Field(min_length=2, max_length=60)
    carrier: str = Field(default="", max_length=120)
    seal_code: str = Field(min_length=4, max_length=80)
    planned_start_at: datetime
    planned_end_at: datetime
    confirm_roles: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def validate_window(self):
        if self.to_station == self.from_station:
            raise ValueError("区段起止站点不能相同")
        if self.planned_end_at <= self.planned_start_at:
            raise ValueError("区段计划到达时间必须晚于计划发车时间")
        return self


class TransportTripCreate(BaseModel):
    case_id: int = Field(gt=0)
    created_by: str = Field(min_length=2, max_length=80)
    idempotency_key: str = Field(min_length=8, max_length=120)
    planned_start_at: datetime
    planned_end_at: datetime
    legs: list[TransportLegPlan] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_chain(self):
        if self.planned_end_at <= self.planned_start_at:
            raise ValueError("行程计划结束时间必须晚于开始时间")
        legs = self.legs
        for index, leg in enumerate(legs):
            if index > 0 and leg.from_station != legs[index - 1].to_station:
                raise ValueError("相邻区段站点必须首尾相接")
            if leg.planned_start_at < self.planned_start_at or leg.planned_end_at > self.planned_end_at:
                raise ValueError("区段计划窗口必须落在行程计划窗口内")
            if index > 0 and leg.planned_start_at < legs[index - 1].planned_start_at:
                raise ValueError("区段必须按时间顺序排列")
        return self


class TransportEventReport(BaseModel):
    actor: str = Field(min_length=2, max_length=80)
    role: str = Field(default="", max_length=60)
    idempotency_key: str = Field(min_length=8, max_length=120)
    occurred_at: datetime
    reported_at: datetime | None = None
    note: str = Field(default="", max_length=1000)
    observed_seal_code: str = Field(default="", max_length=80)
    condition_note: str = Field(default="", max_length=1000)
    reason: str = Field(default="", max_length=500)


class TransportVehicleChange(BaseModel):
    actor: str = Field(min_length=2, max_length=80)
    idempotency_key: str = Field(min_length=8, max_length=120)
    occurred_at: datetime
    reported_at: datetime | None = None
    new_vehicle_code: str = Field(min_length=2, max_length=60)
    new_carrier: str = Field(default="", max_length=120)
    new_seal_code: str = Field(min_length=4, max_length=80)
    reason: str = Field(min_length=2, max_length=500)


class TransportCancel(BaseModel):
    actor: str = Field(min_length=2, max_length=80)
    idempotency_key: str = Field(min_length=8, max_length=120)
    occurred_at: datetime
    reported_at: datetime | None = None
    reason: str = Field(min_length=2, max_length=500)


class TransportLegConfirm(BaseModel):
    confirmed_by: str = Field(min_length=2, max_length=80)
    role: str = Field(min_length=2, max_length=60)
    observed_seal_code: str = Field(min_length=4, max_length=80)
    condition_note: str = Field(default="", max_length=1000)
    idempotency_key: str = Field(min_length=8, max_length=120)


class TransportReroute(BaseModel):
    changed_by: str = Field(min_length=2, max_length=80)
    reason: str = Field(min_length=2, max_length=500)
    idempotency_key: str = Field(min_length=8, max_length=120)
    planned_start_at: datetime | None = None
    planned_end_at: datetime | None = None
    legs: list[TransportLegPlan] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_chain(self):
        legs = self.legs
        for index, leg in enumerate(legs):
            if index > 0 and leg.from_station != legs[index - 1].to_station:
                raise ValueError("相邻区段站点必须首尾相接")
            if leg.planned_end_at <= leg.planned_start_at:
                raise ValueError("区段计划到达时间必须晚于计划发车时间")
            if index > 0 and leg.planned_start_at < legs[index - 1].planned_start_at:
                raise ValueError("区段必须按时间顺序排列")
        if self.planned_start_at and self.planned_end_at and self.planned_end_at <= self.planned_start_at:
            raise ValueError("行程计划结束时间必须晚于开始时间")
        return self
