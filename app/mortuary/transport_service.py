from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from app.core.clock import Clock, SystemClock, from_storage, to_storage
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.database import get_connection, transaction
from app.mortuary.repository import MortuaryRepository

ROUTE_FROZEN_FIELDS = (
    "from_station", "to_station", "planned_depart_at", "planned_arrive_at",
    "seal_code", "confirmer",
)


def _stamp(value: Any) -> str:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return to_storage(value)


def _epoch(value: str | None) -> float | None:
    parsed = from_storage(value)
    return None if parsed is None else parsed.timestamp()


class TransportService:
    """跨区接运行程：按序分段、封签交接、不可变事件账本与责任链。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        self.repository = MortuaryRepository(self.connection)
        self.repository.ensure_schema()

    def now(self) -> str:
        return to_storage(self.clock.now())

    # ------------------------------------------------------------------ 创建
    def create_trip(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = MortuaryRepository(connection)
            case = repo.case(payload["case_id"])
            if case is None:
                raise NotFoundError("逝者业务档案不存在")
            if repo.trip_ref(payload["external_ref"]):
                raise ConflictError("运输行程外部编号已存在")
            duplicate = repo.trip_key(payload["case_id"], payload["idempotency_key"])
            if duplicate:
                return self.get_trip(duplicate["id"], repo)
            plans = payload["segments"]
            cursor = connection.execute(
                "INSERT INTO transport_trips(case_id,external_ref,status,current_sequence,plan_version,origin_station,destination_station,created_by,idempotency_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (payload["case_id"], payload["external_ref"], "planned", 0, 1, plans[0]["from_station"], plans[-1]["to_station"], actor, payload["idempotency_key"], now, now),
            )
            trip_id = int(cursor.lastrowid)
            self._insert_segments(connection, trip_id, 1, plans, now)
            self._snapshot(connection, trip_id, 1, plans, now, reason="", changed_by=actor)
            self._append_event(connection, repo, trip_id, payload["case_id"], None, None, 1, "trip.created", actor, now, now, payload["idempotency_key"], {"external_ref": payload["external_ref"], "segments": len(plans)})
            return self.get_trip(trip_id, repo)

    def _insert_segments(self, connection: sqlite3.Connection, trip_id: int, version: int, plans: list[dict[str, Any]], now: str) -> None:
        for index, plan in enumerate(plans):
            connection.execute(
                "INSERT INTO transport_segments(trip_id,plan_version,sequence_index,from_station,to_station,carrier,vehicle_code,driver,planned_depart_at,planned_arrive_at,seal_code,confirmer,current_seal_code,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (trip_id, version, index, plan["from_station"], plan["to_station"], plan["carrier"], plan.get("vehicle_code", ""), plan.get("driver", ""), _stamp(plan["planned_depart_at"]), _stamp(plan["planned_arrive_at"]), plan["seal_code"], plan["confirmer"], plan["seal_code"], now, now),
            )

    def _snapshot(self, connection: sqlite3.Connection, trip_id: int, version: int, plans: list[dict[str, Any]], now: str, *, reason: str, changed_by: str) -> None:
        serialized = []
        for index, plan in enumerate(plans):
            item = {field: plan[field] for field in ("from_station", "to_station", "carrier", "vehicle_code", "driver", "seal_code", "confirmer")}
            item.update({"sequence_index": index, "planned_depart_at": _stamp(plan["planned_depart_at"]), "planned_arrive_at": _stamp(plan["planned_arrive_at"])})
            serialized.append(item)
        connection.execute(
            "INSERT INTO transport_plan_versions(trip_id,version,reason,changed_by,plan_json,created_at) VALUES(?,?,?,?,?,?)",
            (trip_id, version, reason, changed_by, json.dumps(serialized, ensure_ascii=False, sort_keys=True), now),
        )

    # ------------------------------------------------------------------ 事件
    def _append_event(self, connection: sqlite3.Connection, repo: MortuaryRepository, trip_id: int, case_id: int, segment_id: int | None, sequence_index: int | None, plan_version: int, event_type: str, actor: str, occurred_at: str, reported_at: str, key: str, payload: dict[str, Any]) -> None:
        body = dict(payload)
        body["late_minutes"] = max(0, int((from_storage(reported_at) - from_storage(occurred_at)).total_seconds()) // 60)  # type: ignore[union-attr]
        connection.execute(
            "INSERT INTO transport_events(trip_id,segment_id,plan_version,sequence_index,event_type,actor,occurred_at,reported_at,idempotency_key,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (trip_id, segment_id, plan_version, sequence_index, event_type, actor, occurred_at, reported_at, key, json.dumps(body, ensure_ascii=False, sort_keys=True)),
        )
        mirror = dict(body)
        mirror.update({"trip_id": trip_id})
        if sequence_index is not None:
            mirror["segment"] = sequence_index
        repo.event("case", case_id, f"transport.{event_type}", actor, mirror, reported_at)

    def _dedupe(self, repo: MortuaryRepository, trip_id: int, key: str, event_type: str, occurred_at: str) -> bool:
        existing = repo.transport_event_key(trip_id, key)
        if existing is None:
            return False
        if existing["event_type"] != event_type or existing["occurred_at"] != occurred_at:
            raise ConflictError("同一幂等键已用于其他事件", context={"existing_event": existing["event_type"]})
        return True

    def _load_trip_segment(self, repo: MortuaryRepository, trip_id: int, sequence_index: int) -> tuple[dict[str, Any], dict[str, Any]]:
        trip = repo.trip(trip_id)
        if trip is None:
            raise NotFoundError("运输行程不存在")
        segment = repo.active_segment(trip_id, sequence_index)
        if segment is None:
            raise NotFoundError("行程分段不存在")
        return trip, segment

    # ------------------------------------------------------------------ 发车
    def report_departure(self, trip_id: int, sequence_index: int, payload: dict[str, Any]) -> dict[str, Any]:
        now = self.now()
        occurred = _stamp(payload["occurred_at"])
        if _epoch(occurred) > _epoch(now):
            raise ValidationError("发车发生时间不能晚于报送时间")
        with transaction(immediate=True) as connection:
            repo = MortuaryRepository(connection)
            trip, segment = self._load_trip_segment(repo, trip_id, sequence_index)
            if self._dedupe(repo, trip_id, payload["idempotency_key"], "segment.departed", occurred):
                return self.get_trip(trip_id, repo)
            if trip["status"] in ("completed", "cancelled"):
                raise ConflictError("行程已结束，不能再报发车")
            if segment["status"] != "awaiting":
                raise ConflictError("该分段不是待发车状态", context={"segment_status": segment["status"]})
            if sequence_index == 0:
                if trip["status"] != "planned":
                    raise ConflictError("首段发车时行程必须处于待执行状态")
            else:
                previous = repo.active_segment(trip_id, sequence_index - 1)
                if previous is None or previous["status"] != "confirmed":
                    raise ConflictError("上一段尚未被有权人员确认，本段不能发车")
            seal = payload.get("seal_code") or segment["seal_code"]
            if seal != segment["seal_code"]:
                raise ConflictError("发车时封签编号与计划不一致")
            connection.execute("UPDATE transport_segments SET status='in_progress',actual_depart_at=?,depart_reported_at=?,current_seal_code=?,updated_at=? WHERE id=?", (occurred, now, seal, now, segment["id"]))
            if trip["status"] == "planned":
                connection.execute("UPDATE transport_trips SET status='in_progress',updated_at=? WHERE id=?", (now, trip_id))
            self._append_event(connection, repo, trip_id, trip["case_id"], segment["id"], sequence_index, segment["plan_version"], "segment.departed", payload["actor"], occurred, now, payload["idempotency_key"], {"carrier": segment["carrier"], "vehicle_code": segment["vehicle_code"], "seal_code": seal, "note": payload.get("note", "")})
            return self.get_trip(trip_id, repo)

    # ------------------------------------------------------------------ 到达
    def report_arrival(self, trip_id: int, sequence_index: int, payload: dict[str, Any]) -> dict[str, Any]:
        now = self.now()
        occurred = _stamp(payload["occurred_at"])
        if _epoch(occurred) > _epoch(now):
            raise ValidationError("到达发生时间不能晚于报送时间")
        mismatch: dict[str, str] | None = None
        with transaction(immediate=True) as connection:
            repo = MortuaryRepository(connection)
            trip, segment = self._load_trip_segment(repo, trip_id, sequence_index)
            if self._dedupe(repo, trip_id, payload["idempotency_key"], "segment.arrived", occurred):
                return self.get_trip(trip_id, repo)
            if trip["status"] != "in_progress" or segment["status"] != "in_progress":
                raise ConflictError("该分段不在运输途中，不能报到达")
            if _epoch(occurred) < _epoch(segment["actual_depart_at"]):
                raise ValidationError("到达时间不能早于本段实际发车时间")
            if payload["observed_seal_code"] != segment["current_seal_code"]:
                # 封签异常本身也是不可覆盖的事件：先落账提交，再拒绝状态推进
                self._append_event(connection, repo, trip_id, trip["case_id"], segment["id"], sequence_index, segment["plan_version"], "segment.seal_mismatch", payload["actor"], occurred, now, payload["idempotency_key"], {"expected_seal_code": segment["current_seal_code"], "observed_seal_code": payload["observed_seal_code"], "condition_note": payload.get("condition_note", "")})
                mismatch = {"expected": segment["current_seal_code"], "observed": payload["observed_seal_code"]}
            else:
                connection.execute("UPDATE transport_segments SET status='arrived',actual_arrive_at=?,arrive_reported_at=?,updated_at=? WHERE id=?", (occurred, now, now, segment["id"]))
                self._append_event(connection, repo, trip_id, trip["case_id"], segment["id"], sequence_index, segment["plan_version"], "segment.arrived", payload["actor"], occurred, now, payload["idempotency_key"], {"station": segment["to_station"], "seal_code": payload["observed_seal_code"], "condition_note": payload.get("condition_note", "")})
                return self.get_trip(trip_id, repo)
        raise ConflictError("封签编号不一致，到达未被确认，请核查责任", context=mismatch)

    # ------------------------------------------------------------------ 迟到
    def report_delay(self, trip_id: int, sequence_index: int, payload: dict[str, Any]) -> dict[str, Any]:
        now = self.now()
        occurred = _stamp(payload["occurred_at"])
        return self._report_in_segment(trip_id, sequence_index, payload, occurred, now, "segment.delayed", {
            "delay_minutes": payload["delay_minutes"], "reason": payload["reason"],
        })

    def report_abnormal_stop(self, trip_id: int, sequence_index: int, payload: dict[str, Any]) -> dict[str, Any]:
        now = self.now()
        occurred = _stamp(payload["occurred_at"])
        resumed = _stamp(payload["resumed_at"]) if payload.get("resumed_at") else None
        if resumed and _epoch(resumed) > _epoch(now):
            raise ValidationError("恢复通行时间不能晚于报送时间")
        body: dict[str, Any] = {"location": payload["location"], "reason": payload["reason"], "note": payload.get("note", "")}
        if resumed:
            body["resumed_at"] = resumed
        return self._report_in_segment(trip_id, sequence_index, payload, occurred, now, "segment.abnormal_stop", body)

    def report_vehicle_change(self, trip_id: int, sequence_index: int, payload: dict[str, Any]) -> dict[str, Any]:
        now = self.now()
        occurred = _stamp(payload["occurred_at"])
        if _epoch(occurred) > _epoch(now):
            raise ValidationError("换车发生时间不能晚于报送时间")
        with transaction(immediate=True) as connection:
            repo = MortuaryRepository(connection)
            trip, segment = self._load_trip_segment(repo, trip_id, sequence_index)
            if self._dedupe(repo, trip_id, payload["idempotency_key"], "segment.vehicle_changed", occurred):
                return self.get_trip(trip_id, repo)
            if trip["status"] != "in_progress" or segment["status"] != "in_progress":
                raise ConflictError("只有运输途中的分段可以换车换封签")
            new_carrier = payload.get("new_carrier") or segment["carrier"]
            connection.execute("UPDATE transport_segments SET carrier=?,vehicle_code=?,driver=?,current_seal_code=?,updated_at=? WHERE id=?", (new_carrier, payload["new_vehicle_code"], payload.get("new_driver", ""), payload["new_seal_code"], now, segment["id"]))
            self._append_event(connection, repo, trip_id, trip["case_id"], segment["id"], sequence_index, segment["plan_version"], "segment.vehicle_changed", payload["actor"], occurred, now, payload["idempotency_key"], {"previous_carrier": segment["carrier"], "carrier": new_carrier, "previous_vehicle_code": segment["vehicle_code"], "vehicle_code": payload["new_vehicle_code"], "previous_seal_code": segment["current_seal_code"], "seal_code": payload["new_seal_code"], "reason": payload["reason"]})
            return self.get_trip(trip_id, repo)

    def _report_in_segment(self, trip_id: int, sequence_index: int, payload: dict[str, Any], occurred: str, now: str, event_type: str, body: dict[str, Any]) -> dict[str, Any]:
        if _epoch(occurred) > _epoch(now):
            raise ValidationError("事件发生时间不能晚于报送时间")
        with transaction(immediate=True) as connection:
            repo = MortuaryRepository(connection)
            trip, segment = self._load_trip_segment(repo, trip_id, sequence_index)
            if self._dedupe(repo, trip_id, payload["idempotency_key"], event_type, occurred):
                return self.get_trip(trip_id, repo)
            if trip["status"] == "cancelled":
                raise ConflictError("行程已取消")
            if segment["status"] not in ("in_progress", "arrived"):
                raise ConflictError("途中事件只能发生在尚未确认的运输分段", context={"segment_status": segment["status"]})
            if not segment["actual_depart_at"] or _epoch(occurred) < _epoch(segment["actual_depart_at"]):
                raise ValidationError("途中事件不能早于本段实际发车时间")
            if segment["actual_arrive_at"] and _epoch(occurred) > _epoch(segment["actual_arrive_at"]):
                raise ValidationError("途中事件不能晚于本段实际到达时间")
            self._append_event(connection, repo, trip_id, trip["case_id"], segment["id"], sequence_index, segment["plan_version"], event_type, payload["actor"], occurred, now, payload["idempotency_key"], body)
            return self.get_trip(trip_id, repo)

    # ------------------------------------------------------------------ 确认
    def confirm_segment(self, trip_id: int, sequence_index: int, payload: dict[str, Any]) -> dict[str, Any]:
        now = self.now()
        occurred = _stamp(payload["occurred_at"]) if payload.get("occurred_at") else now
        with transaction(immediate=True) as connection:
            repo = MortuaryRepository(connection)
            trip, segment = self._load_trip_segment(repo, trip_id, sequence_index)
            if self._dedupe(repo, trip_id, payload["idempotency_key"], "segment.confirmed", occurred):
                return self.get_trip(trip_id, repo)
            if trip["status"] == "cancelled":
                raise ConflictError("行程已取消")
            if segment["status"] != "arrived":
                raise ConflictError("只有已到站待确认的分段可以确认", context={"segment_status": segment["status"]})
            if payload["confirmed_by"] != segment["confirmer"]:
                raise PermissionDeniedError("只有该分段指定的有权确认人可以确认")
            if payload["observed_seal_code"] != segment["current_seal_code"]:
                raise ConflictError("确认时封签编号不一致")
            connection.execute("UPDATE transport_segments SET status='confirmed',confirmed_by=?,confirmed_at=?,updated_at=? WHERE id=?", (payload["confirmed_by"], occurred, now, segment["id"]))
            segments = repo.active_segments(trip_id)
            is_final = sequence_index == max(item["sequence_index"] for item in segments)
            if is_final:
                connection.execute("UPDATE transport_trips SET status='completed',current_sequence=?,updated_at=? WHERE id=?", (sequence_index + 1, now, trip_id))
                connection.execute("UPDATE mortuary_cases SET status='in_custody',current_location=?,version=version+1,updated_at=? WHERE id=?", (segment["to_station"], now, trip["case_id"]))
            else:
                connection.execute("UPDATE transport_trips SET current_sequence=?,updated_at=? WHERE id=?", (sequence_index + 1, now, trip_id))
            self._append_event(connection, repo, trip_id, trip["case_id"], segment["id"], sequence_index, segment["plan_version"], "segment.confirmed", payload["confirmed_by"], occurred, now, payload["idempotency_key"], {"station": segment["to_station"], "seal_code": payload["observed_seal_code"], "final": is_final, "condition_note": payload.get("condition_note", "")})
            return self.get_trip(trip_id, repo)

    # ------------------------------------------------------------------ 取消
    def cancel_trip(self, trip_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        now = self.now()
        occurred = _stamp(payload["occurred_at"]) if payload.get("occurred_at") else now
        with transaction(immediate=True) as connection:
            repo = MortuaryRepository(connection)
            trip = repo.trip(trip_id)
            if trip is None:
                raise NotFoundError("运输行程不存在")
            if self._dedupe(repo, trip_id, payload["idempotency_key"], "trip.cancelled", occurred):
                return self.get_trip(trip_id, repo)
            if trip["status"] == "completed":
                raise ConflictError("已完成的行程不能取消")
            if trip["status"] == "cancelled":
                return self.get_trip(trip_id, repo)
            connection.execute("UPDATE transport_trips SET status='cancelled',cancel_reason=?,cancelled_by=?,updated_at=? WHERE id=?", (payload["reason"], payload["actor"], now, trip_id))
            connection.execute("UPDATE transport_segments SET status='cancelled',updated_at=? WHERE trip_id=? AND superseded=0 AND status IN ('awaiting','in_progress')", (now, trip_id))
            resting = connection.execute("SELECT to_station FROM transport_segments WHERE trip_id=? AND superseded=0 AND status IN ('arrived','confirmed') ORDER BY sequence_index DESC LIMIT 1", (trip_id,)).fetchone()
            if resting is not None:
                connection.execute("UPDATE mortuary_cases SET current_location=?,version=version+1,updated_at=? WHERE id=?", (resting["to_station"], now, trip["case_id"]))
            self._append_event(connection, repo, trip_id, trip["case_id"], None, None, trip["plan_version"], "trip.cancelled", payload["actor"], occurred, now, payload["idempotency_key"], {"reason": payload["reason"]})
            return self.get_trip(trip_id, repo)

    # ------------------------------------------------------------------ 改线
    def reroute(self, trip_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = MortuaryRepository(connection)
            trip = repo.trip(trip_id)
            if trip is None:
                raise NotFoundError("运输行程不存在")
            if repo.transport_event_key(trip_id, payload["idempotency_key"]):
                return self.get_trip(trip_id, repo)
            if trip["status"] in ("completed", "cancelled"):
                raise ConflictError("已结束的行程不能变更路线")
            old_segments = repo.active_segments(trip_id)
            fixed = [item for item in old_segments if item["status"] in ("confirmed", "arrived", "in_progress")]
            new_plans = payload["segments"]
            if len(new_plans) <= len(fixed):
                raise ValidationError("改线必须至少保留一个尚未发车的分段")
            for index, existing in enumerate(fixed):
                plan = new_plans[index]
                for field in ROUTE_FROZEN_FIELDS:
                    candidate = _stamp(plan[field]) if field.endswith("_at") else plan[field]
                    if candidate != existing[field]:
                        raise ConflictError("已发生分段的路线信息不允许修改", context={"sequence": index, "field": field})
            if new_plans[len(fixed)]["from_station"] != (fixed[-1]["to_station"] if fixed else trip["origin_station"]):
                raise ValidationError("新路线必须从尚未完成的衔接站点开始")
            change_point = old_segments[len(fixed)]["planned_depart_at"] if len(fixed) < len(old_segments) else now
            new_version = int(trip["plan_version"]) + 1
            normalized = self._normalize_plans(new_plans)
            # 原计划整版保留（快照已在创建/上次变更时保存），仅替换尚未发车的分段
            connection.execute("UPDATE transport_segments SET superseded=1,updated_at=? WHERE trip_id=? AND superseded=0 AND status='awaiting'", (now, trip_id))
            tail = normalized[len(fixed):]
            self._insert_tail(connection, trip_id, new_version, len(fixed), tail, now)
            destination = normalized[-1]["to_station"]
            connection.execute("UPDATE transport_trips SET plan_version=?,destination_station=?,updated_at=? WHERE id=?", (new_version, destination, now, trip_id))
            self._snapshot(connection, trip_id, new_version, normalized, now, reason=payload["reason"], changed_by=payload["actor"])
            self._append_event(connection, repo, trip_id, trip["case_id"], None, None, new_version, "trip.rerouted", payload["actor"], now, now, payload["idempotency_key"], {"reason": payload["reason"], "new_version": new_version, "kept_segments": len(fixed)})
            self._recompute_impacts(connection, trip_id, trip["case_id"], old_segments, normalized, new_version, change_point, now)
            return self.get_trip(trip_id, repo)

    def _normalize_plans(self, plans: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result = []
        for plan in plans:
            item = dict(plan)
            item["planned_depart_at"] = _stamp(plan["planned_depart_at"])
            item["planned_arrive_at"] = _stamp(plan["planned_arrive_at"])
            result.append(item)
        return result

    def _insert_tail(self, connection: sqlite3.Connection, trip_id: int, version: int, offset: int, plans: list[dict[str, Any]], now: str) -> None:
        for shift, plan in enumerate(plans):
            connection.execute(
                "INSERT INTO transport_segments(trip_id,plan_version,sequence_index,from_station,to_station,carrier,vehicle_code,driver,planned_depart_at,planned_arrive_at,seal_code,confirmer,current_seal_code,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (trip_id, version, offset + shift, plan["from_station"], plan["to_station"], plan["carrier"], plan.get("vehicle_code", ""), plan.get("driver", ""), plan["planned_depart_at"], plan["planned_arrive_at"], plan["seal_code"], plan["confirmer"], plan["seal_code"], now, now),
            )

    def _recompute_impacts(self, connection: sqlite3.Connection, trip_id: int, case_id: int, old_segments: list[dict[str, Any]], new_plans: list[dict[str, Any]], version: int, change_point: str, now: str) -> None:
        reservations = connection.execute("SELECT r.*,f.code resource_code FROM facility_reservations r JOIN facility_resources f ON f.id=r.resource_id WHERE r.case_id=? AND r.status='confirmed' ORDER BY r.start_at", (case_id,)).fetchall()
        for row in reservations:
            reservation = dict(row)
            if reservation["start_at"] < change_point:
                continue
            old_station, old_eta = self._station_at(old_segments, reservation["start_at"])
            new_station, new_eta = self._station_at(new_plans, reservation["start_at"])
            if old_station == new_station and old_eta == new_eta:
                continue
            impact = "needs_reschedule" if old_station != new_station else "time_shifted"
            connection.execute(
                "INSERT INTO transport_reservation_impacts(trip_id,plan_version,reservation_id,resource_code,scheduled_start_at,new_eta_at,impact,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (trip_id, version, reservation["id"], reservation["resource_code"], reservation["start_at"], new_eta, impact, now),
            )

    @staticmethod
    def _station_at(segments: list[dict[str, Any]], at: str) -> tuple[str, str]:
        """给出在某时刻按计划遗体应处的站点，以及到达该站点的计划时间。"""
        if not segments:
            return "", at
        current_station = segments[0]["from_station"]
        current_eta = segments[0]["planned_depart_at"]
        for segment in segments:
            if at >= segment["planned_arrive_at"]:
                current_station = segment["to_station"]
                current_eta = segment["planned_arrive_at"]
            else:
                break
        return current_station, current_eta

    # ------------------------------------------------------------------ 查询
    def list_trips(self, status: str | None = None, case_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
        sql = "SELECT * FROM transport_trips WHERE 1=1"
        params: list[Any] = []
        if status:
            sql += " AND status=?"
            params.append(status)
        if case_id is not None:
            sql += " AND case_id=?"
            params.append(case_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        return [dict(row) for row in self.connection.execute(sql, params).fetchall()]

    def get_trip(self, trip_id: int, repo: MortuaryRepository | None = None) -> dict[str, Any]:
        repo = repo or self.repository
        trip = repo.trip(trip_id)
        if trip is None:
            raise NotFoundError("运输行程不存在")
        segments = repo.active_segments(trip_id)
        events = repo.transport_events(trip_id)
        now = self.now()
        current = None
        for segment in segments:
            if segment["status"] in ("in_progress", "arrived"):
                current = segment
                break
        if current is None:
            for segment in reversed(segments):
                if segment["status"] == "confirmed":
                    current = segment
                    break
        view = dict(trip)
        view["segments"] = [self._segment_view(segment, events) for segment in segments]
        view["events"] = events
        view["responsibility_chain"] = self._chain(segments, events)
        view["plan_versions"] = repo.plan_versions(trip_id)
        view["reservation_impacts"] = repo.reservation_impacts(trip_id)
        view["current_carrier"] = current["carrier"] if current else None
        view["current_vehicle_code"] = current["vehicle_code"] if current else None
        view["current_driver"] = current["driver"] if current else None
        view["current_location"] = self._current_location(trip, segments, current)
        view["pending_confirmations"] = [
            {"segment_id": segment["id"], "sequence_index": segment["sequence_index"], "station": segment["to_station"], "confirmer": segment["confirmer"], "arrived_at": segment["actual_arrive_at"], "arrive_reported_at": segment["arrive_reported_at"]}
            for segment in segments if segment["status"] == "arrived"
        ]
        view["overtimes"] = [overtime for overtime in (self._overtime(segment, events, now) for segment in segments) if overtime is not None]
        return view

    @staticmethod
    def _current_location(trip: dict[str, Any], segments: list[dict[str, Any]], current: dict[str, Any] | None) -> str:
        if trip["status"] == "completed":
            return trip["destination_station"]
        if current is None:
            return trip["origin_station"]
        if current["status"] == "in_progress":
            return f"{current['from_station']}→{current['to_station']}"
        return current["to_station"]

    def _segment_view(self, segment: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
        item = dict(segment)
        item["superseded"] = bool(segment["superseded"])
        item["events"] = [event["event_type"] for event in events if event["segment_id"] == segment["id"]]
        return item

    @staticmethod
    def _chain(segments: list[dict[str, Any]], events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        station_by_id = {segment["id"]: {"from": segment["from_station"], "to": segment["to_station"], "carrier": segment["carrier"], "vehicle_code": segment["vehicle_code"]} for segment in segments}
        chain = []
        for event in sorted(events, key=lambda item: (item["occurred_at"], item["id"])):
            node = {
                "sequence_index": event["sequence_index"],
                "event_type": event["event_type"],
                "actor": event["actor"],
                "occurred_at": event["occurred_at"],
                "reported_at": event["reported_at"],
                "late_minutes": event["payload"].get("late_minutes", 0),
                "payload": event["payload"],
            }
            context = station_by_id.get(event["segment_id"])
            if context:
                node.update(context)
            chain.append(node)
        return chain

    @staticmethod
    def _overtime(segment: dict[str, Any], events: list[dict[str, Any]], now: str) -> dict[str, Any] | None:
        def late(actual: str | None, planned: str, open_ended: bool = False) -> int:
            reference = actual if actual else (now if open_ended else None)
            if reference is None:
                return 0
            return max(0, int(_epoch(reference) - _epoch(planned)))  # type: ignore[arg-type]

        depart_open = segment["status"] in ("awaiting",) and not segment["actual_depart_at"]
        arrive_open = segment["status"] in ("in_progress",) and not segment["actual_arrive_at"]
        depart_seconds = late(segment["actual_depart_at"], segment["planned_depart_at"], depart_open)
        arrive_seconds = late(segment["actual_arrive_at"], segment["planned_arrive_at"], arrive_open)
        delay_events = [
            {"sequence_index": event["sequence_index"], "occurred_at": event["occurred_at"], "reported_at": event["reported_at"], "minutes": event["payload"].get("delay_minutes", 0), "reason": event["payload"].get("reason", ""), "actor": event["actor"]}
            for event in events if event["segment_id"] == segment["id"] and event["event_type"] == "segment.delayed"
        ]
        if depart_seconds == 0 and arrive_seconds == 0 and not delay_events:
            return None
        return {
            "sequence_index": segment["sequence_index"],
            "segment_id": segment["id"],
            "carrier": segment["carrier"],
            "vehicle_code": segment["vehicle_code"],
            "status": segment["status"],
            "departure_overdue_seconds": depart_seconds,
            "arrival_overdue_seconds": arrive_seconds,
            "reported_delay_minutes": sum(item["minutes"] for item in delay_events),
            "delay_events": delay_events,
        }

    def coordination_overview(self, status: str | None = "in_progress", overdue_only: bool = False) -> dict[str, Any]:
        trips = self.list_trips(status, limit=500)
        items = []
        for trip_row in trips:
            detail = self.get_trip(trip_row["id"])
            if overdue_only and not detail["overtimes"] and not detail["pending_confirmations"]:
                continue
            items.append({
                "trip_id": detail["id"],
                "external_ref": detail["external_ref"],
                "case_id": detail["case_id"],
                "status": detail["status"],
                "current_carrier": detail["current_carrier"],
                "current_vehicle_code": detail["current_vehicle_code"],
                "current_location": detail["current_location"],
                "current_sequence": detail["current_sequence"],
                "pending_confirmations": detail["pending_confirmations"],
                "overtimes": detail["overtimes"],
            })
        return {"trips": items, "count": len(items)}
