from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, model_validator


class TransportSegmentPlan(BaseModel):
    from_station: str = Field(min_length=2, max_length=120)
    to_station: str = Field(min_length=2, max_length=120)
    carrier: str = Field(min_length=2, max_length=120)
    vehicle_code: str = Field(default="", max_length=80)
    driver: str = Field(default="", max_length=80)
    planned_depart_at: datetime
    planned_arrive_at: datetime
    seal_code: str = Field(min_length=4, max_length=80)
    confirmer: str = Field(min_length=2, max_length=80)

    @model_validator(mode="after")
    def window_must_be_ordered(self):
        if self.to_station == self.from_station:
            raise ValueError("单段起点和终点不能相同")
        if self.planned_arrive_at <= self.planned_depart_at:
            raise ValueError("计划到达时间必须晚于计划发车时间")
        return self


class TransportTripCreate(BaseModel):
    external_ref: str = Field(min_length=3, max_length=80)
    case_id: int = Field(gt=0)
    created_by: str = Field(min_length=2, max_length=80)
    idempotency_key: str = Field(min_length=8, max_length=120)
    segments: list[TransportSegmentPlan] = Field(min_length=1, max_length=40)

    @model_validator(mode="after")
    def segments_must_chain(self):
        segments = self.segments
        for previous, current in zip(segments, segments[1:]):
            if previous.to_station != current.from_station:
                raise ValueError("相邻分段的站点必须首尾相接")
            if current.planned_depart_at < previous.planned_arrive_at:
                raise ValueError("后一段计划发车时间不得早于前一段计划到达时间")
        return self


class DepartReport(BaseModel):
    actor: str = Field(min_length=2, max_length=80)
    idempotency_key: str = Field(min_length=8, max_length=120)
    occurred_at: datetime
    seal_code: str | None = Field(default=None, max_length=80)
    note: str = Field(default="", max_length=1000)


class ArriveReport(BaseModel):
    actor: str = Field(min_length=2, max_length=80)
    idempotency_key: str = Field(min_length=8, max_length=120)
    occurred_at: datetime
    observed_seal_code: str = Field(min_length=4, max_length=80)
    condition_note: str = Field(default="", max_length=1000)


class DelayReport(BaseModel):
    actor: str = Field(min_length=2, max_length=80)
    idempotency_key: str = Field(min_length=8, max_length=120)
    occurred_at: datetime
    delay_minutes: int = Field(gt=0, le=100000)
    reason: str = Field(min_length=2, max_length=500)


class AbnormalStopReport(BaseModel):
    actor: str = Field(min_length=2, max_length=80)
    idempotency_key: str = Field(min_length=8, max_length=120)
    occurred_at: datetime
    location: str = Field(min_length=2, max_length=160)
    reason: str = Field(min_length=2, max_length=500)
    resumed_at: datetime | None = None
    note: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def resume_after_stop(self):
        if self.resumed_at is not None and self.resumed_at < self.occurred_at:
            raise ValueError("恢复通行时间不能早于异常停留开始时间")
        return self


class VehicleChangeReport(BaseModel):
    actor: str = Field(min_length=2, max_length=80)
    idempotency_key: str = Field(min_length=8, max_length=120)
    occurred_at: datetime
    new_carrier: str | None = Field(default=None, min_length=2, max_length=120)
    new_vehicle_code: str = Field(min_length=1, max_length=80)
    new_driver: str = Field(default="", max_length=80)
    new_seal_code: str = Field(min_length=4, max_length=80)
    reason: str = Field(min_length=2, max_length=500)


class SegmentConfirm(BaseModel):
    confirmed_by: str = Field(min_length=2, max_length=80)
    idempotency_key: str = Field(min_length=8, max_length=120)
    observed_seal_code: str = Field(min_length=4, max_length=80)
    condition_note: str = Field(default="", max_length=1000)
    occurred_at: datetime | None = None


class TripCancel(BaseModel):
    actor: str = Field(min_length=2, max_length=80)
    idempotency_key: str = Field(min_length=8, max_length=120)
    reason: str = Field(min_length=2, max_length=500)
    occurred_at: datetime | None = None


class TransportReroute(BaseModel):
    actor: str = Field(min_length=2, max_length=80)
    idempotency_key: str = Field(min_length=8, max_length=120)
    reason: str = Field(min_length=2, max_length=500)
    segments: list[TransportSegmentPlan] = Field(min_length=1, max_length=40)

    @model_validator(mode="after")
    def segments_must_chain(self):
        segments = self.segments
        for previous, current in zip(segments, segments[1:]):
            if previous.to_station != current.from_station:
                raise ValueError("相邻分段的站点必须首尾相接")
            if current.planned_depart_at < previous.planned_arrive_at:
                raise ValueError("后一段计划发车时间不得早于前一段计划到达时间")
        return self
