from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.core.clock import Clock, SystemClock, from_storage, to_storage
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.database import get_connection, transaction
from app.mortuary.repository import MortuaryRepository

# 行程状态
TRIP_ACTIVE = ("planned", "in_progress")
LIVE_EVENT_TYPES = {
    "departed": "leg.departed",
    "arrived": "leg.arrived",
    "abnormal_stop": "leg.abnormal_stop",
    "resumed": "leg.resumed",
}


class TransportService:
    """多段运输行程：按序站点、车辆、计划窗口与封签，事件只追加、不可覆盖。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        self.repository = MortuaryRepository(self.connection)
        self.repository.ensure_schema()

    def now(self) -> str:
        return to_storage(self.clock.now())

    # ------------------------------------------------------------------ 创建

    def create_trip(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = self.now()
        planned_start = to_storage(payload["planned_start_at"])
        planned_end = to_storage(payload["planned_end_at"])
        legs = payload["legs"]
        with transaction(immediate=True) as connection:
            repo = MortuaryRepository(connection)
            case = repo.case(payload["case_id"])
            if case is None:
                raise NotFoundError("逝者业务档案不存在")
            existing = repo.trip_key(payload["case_id"], payload["idempotency_key"])
            if existing:
                return self._assemble(repo, existing)
            if repo.active_trip_for_case(payload["case_id"]) is not None:
                raise ConflictError("该业务档案已有进行中的运输行程")
            cursor = connection.execute(
                "INSERT INTO transport_trips(case_id,origin_station,destination_station,status,route_version,"
                "planned_start_at,planned_end_at,created_by,idempotency_key,created_at,updated_at) "
                "VALUES(?,?,?,?,1,?,?,?,?,?,?)",
                (
                    payload["case_id"], legs[0]["from_station"], legs[-1]["to_station"], "planned",
                    planned_start, planned_end, payload["created_by"], payload["idempotency_key"], now, now,
                ),
            )
            trip_id = int(cursor.lastrowid)
            self._insert_leg_rows(connection, trip_id, 1, legs, now)
            self._snapshot_plan(connection, repo, trip_id, 1, legs, payload["created_by"], "初始计划", now)
            repo.event("case", payload["case_id"], "trip.registered", payload["created_by"],
                       {"trip_id": trip_id, "legs": len(legs), "planned_end_at": planned_end}, now)
            self._append_event(connection, trip_id, None, 1, None, "trip.created",
                               payload["created_by"], planned_start, now, payload["idempotency_key"],
                               {"origin": legs[0]["from_station"], "destination": legs[-1]["to_station"],
                                "planned_start_at": planned_start, "planned_end_at": planned_end}, now)
            return self._assemble(repo, repo.trip(trip_id) or {})

    # ------------------------------------------------------------------ 查询

    def get_trip(self, trip_id: int) -> dict[str, Any]:
        trip = self.repository.trip(trip_id)
        if trip is None:
            raise NotFoundError("运输行程不存在")
        return self._assemble(self.repository, trip)

    def list_trips(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return self.repository.list_trips(status, max(1, min(limit, 500)))

    def overview(self) -> dict[str, Any]:
        """协调员视图：当前承运方、超时段、待确认节点。"""
        overdue: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        active: list[dict[str, Any]] = []
        for trip in self.repository.list_trips(None, 500):
            if trip["status"] not in TRIP_ACTIVE:
                continue
            detail = self._assemble(self.repository, trip)
            active.append({
                "trip_id": trip["id"], "case_id": trip["case_id"], "status": trip["status"],
                "route_version": trip["route_version"], "current_carrier": detail["current_carrier"],
            })
            for leg in detail["legs"]:
                if leg["minutes_late"] is not None and leg["status"] != "cancelled":
                    overdue.append({"trip_id": trip["id"], "case_id": trip["case_id"], "seq": leg["seq"],
                                    "from_station": leg["from_station"], "to_station": leg["to_station"],
                                    "vehicle_code": leg["vehicle_code"], "carrier": leg["carrier"],
                                    "status": leg["status"], "minutes_late": leg["minutes_late"]})
                if leg["status"] == "arrived":
                    pending.append({"trip_id": trip["id"], "case_id": trip["case_id"], "seq": leg["seq"],
                                    "to_station": leg["to_station"], "vehicle_code": leg["vehicle_code"],
                                    "carrier": leg["carrier"], "seal_code": leg["seal_code"],
                                    "arrived_at": leg["arrived_at"], "waiting_minutes": leg["waiting_minutes"]})
        overdue.sort(key=lambda item: item["minutes_late"], reverse=True)
        return {"active_trips": active, "overdue_legs": overdue, "pending_confirmations": pending}

    # ------------------------------------------------------------------ 事件

    def report_event(self, trip_id: int, seq: int, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        if event_type not in LIVE_EVENT_TYPES:
            raise ValidationError("不支持的行程事件类型")
        now = self.now()
        occurred = to_storage(payload["occurred_at"])
        reported = to_storage(payload["reported_at"]) if payload.get("reported_at") else now
        if from_storage(reported) < from_storage(occurred):
            raise ValidationError("报送时间不能早于事件发生时间")
        with transaction(immediate=True) as connection:
            repo = MortuaryRepository(connection)
            trip = self._require_active_trip(repo, trip_id)
            leg = self._require_leg(repo, trip, seq)
            duplicate = repo.transport_event_key(trip_id, LIVE_EVENT_TYPES[event_type], payload["idempotency_key"])
            if duplicate:  # 重复回报不重复推进状态
                return self._assemble(repo, trip)
            event_payload: dict[str, Any] = {"note": payload.get("note", "")}
            if event_type == "departed":
                self._do_depart(connection, repo, trip, leg, payload, occurred, now)
            elif event_type == "arrived":
                self._do_arrive(repo, trip, leg, payload, occurred)
            elif event_type == "abnormal_stop":
                self._do_stop(repo, trip, leg, payload, occurred)
                event_payload["reason"] = payload.get("reason", "")
            else:  # resumed
                self._do_resume(repo, trip, leg, occurred)
            self._append_event(connection, trip_id, leg["id"], trip["route_version"], seq,
                               LIVE_EVENT_TYPES[event_type], payload["actor"], occurred, reported,
                               payload["idempotency_key"], event_payload, now)
            connection.execute("UPDATE transport_trips SET status='in_progress',updated_at=? WHERE id=? AND status='planned'", (now, trip_id))
            return self._assemble(repo, repo.trip(trip_id) or {})

    def change_vehicle(self, trip_id: int, seq: int, payload: dict[str, Any]) -> dict[str, Any]:
        """中途换车、换封签：旧承运信息留在事件里，责任不被覆盖。"""
        now = self.now()
        occurred = to_storage(payload["occurred_at"])
        reported = to_storage(payload["reported_at"]) if payload.get("reported_at") else now
        if from_storage(reported) < from_storage(occurred):
            raise ValidationError("报送时间不能早于事件发生时间")
        with transaction(immediate=True) as connection:
            repo = MortuaryRepository(connection)
            trip = self._require_active_trip(repo, trip_id)
            leg = self._require_leg(repo, trip, seq)
            duplicate = repo.transport_event_key(trip_id, "leg.vehicle_changed", payload["idempotency_key"])
            if duplicate:
                return self._assemble(repo, trip)
            if leg["status"] not in ("in_transit", "stopped"):
                raise ConflictError("只有在途区段可以换车")
            if from_storage(occurred) < from_storage(leg["departed_at"]):
                raise ValidationError("换车时间不能早于本区段发车时间")
            old_vehicle, old_seal, old_carrier = leg["vehicle_code"], leg["seal_code"], leg["carrier"]
            connection.execute(
                "UPDATE transport_legs SET vehicle_code=?,carrier=?,seal_code=?,updated_at=? WHERE id=?",
                (payload["new_vehicle_code"], payload.get("new_carrier", ""), payload["new_seal_code"], now, leg["id"]),
            )
            self._append_event(connection, trip_id, leg["id"], trip["route_version"], seq,
                               "leg.vehicle_changed", payload["actor"], occurred, reported,
                               payload["idempotency_key"],
                               {"old_vehicle_code": old_vehicle, "old_carrier": old_carrier, "old_seal_code": old_seal,
                                "new_vehicle_code": payload["new_vehicle_code"],
                                "new_carrier": payload.get("new_carrier", ""),
                                "new_seal_code": payload["new_seal_code"], "reason": payload["reason"]}, now)
            return self._assemble(repo, repo.trip(trip_id) or {})

    def confirm_leg(self, trip_id: int, seq: int, payload: dict[str, Any]) -> dict[str, Any]:
        """上一段必须由有权人员确认封签后，下一段才能发车。"""
        now = self.now()
        rejection: ConflictError | None = None
        with transaction(immediate=True) as connection:
            repo = MortuaryRepository(connection)
            trip = self._require_active_trip(repo, trip_id)
            leg = self._require_leg(repo, trip, seq)
            duplicate = repo.transport_event_key(trip_id, "leg.confirmed", payload["idempotency_key"])
            if duplicate:
                return self._assemble(repo, trip)
            if leg["status"] == "rejected":
                raise ConflictError("该区段封签核验已被拒收，需改线或取消后才能继续")
            if leg["status"] != "arrived":
                raise ConflictError("只有已到达的区段可以确认")
            roles = json.loads(leg["confirm_roles_json"])
            if roles and payload["role"] not in roles:
                raise PermissionDeniedError("当前角色无权确认该区段", context={"required_roles": roles})
            if payload["observed_seal_code"] != leg["seal_code"]:
                connection.execute(
                    "UPDATE transport_legs SET status='rejected',observed_seal_code=?,condition_note=?,"
                    "rejected_at=?,updated_at=? WHERE id=?",
                    (payload["observed_seal_code"], payload["condition_note"], now, now, leg["id"]),
                )
                self._append_event(connection, trip_id, leg["id"], trip["route_version"], seq,
                                   "leg.rejected", payload["confirmed_by"], now, now,
                                   payload["idempotency_key"],
                                   {"expected_seal_code": leg["seal_code"],
                                    "observed_seal_code": payload["observed_seal_code"]}, now)
                rejection = ConflictError("封签编号不一致，区段交接已拒收")
            else:
                connection.execute(
                    "UPDATE transport_legs SET status='confirmed',confirmed_by=?,confirmer_role=?,"
                    "observed_seal_code=?,condition_note=?,confirmed_at=?,updated_at=? WHERE id=?",
                    (payload["confirmed_by"], payload["role"], payload["observed_seal_code"],
                     payload["condition_note"], now, now, leg["id"]),
                )
                self._append_event(connection, trip_id, leg["id"], trip["route_version"], seq,
                                   "leg.confirmed", payload["confirmed_by"], now, now,
                                   payload["idempotency_key"],
                                   {"role": payload["role"], "seal_code": payload["observed_seal_code"],
                                    "to_station": leg["to_station"]}, now)
                legs = repo.legs(trip_id, trip["route_version"])
                if all(item["status"] == "confirmed" for item in legs):
                    connection.execute(
                        "UPDATE transport_trips SET status='completed',completed_at=?,updated_at=? WHERE id=?",
                        (now, now, trip_id),
                    )
                    connection.execute(
                        "UPDATE mortuary_cases SET status='in_custody',current_location=?,version=version+1,updated_at=? WHERE id=?",
                        (trip["destination_station"], now, trip["case_id"]),
                    )
                    repo.event("case", trip["case_id"], "trip.completed", payload["confirmed_by"],
                               {"trip_id": trip_id, "destination": trip["destination_station"]}, now)
                    self._append_event(connection, trip_id, None, trip["route_version"], None,
                                       "trip.completed", payload["confirmed_by"], now, now,
                                       f"complete-{trip_id}-{payload['idempotency_key']}", {}, now)
            result = self._assemble(repo, repo.trip(trip_id) or {})
        if rejection is not None:
            raise rejection
        return result

    def cancel_trip(self, trip_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        now = self.now()
        occurred = to_storage(payload["occurred_at"])
        reported = to_storage(payload["reported_at"]) if payload.get("reported_at") else now
        if from_storage(reported) < from_storage(occurred):
            raise ValidationError("报送时间不能早于事件发生时间")
        with transaction(immediate=True) as connection:
            repo = MortuaryRepository(connection)
            trip = self._require_active_trip(repo, trip_id)
            duplicate = repo.transport_event_key(trip_id, "trip.cancelled", payload["idempotency_key"])
            if duplicate:
                return self._assemble(repo, trip)
            connection.execute(
                "UPDATE transport_trips SET status='cancelled',cancelled_at=?,cancel_reason=?,updated_at=? WHERE id=?",
                (occurred, payload["reason"], now, trip_id),
            )
            connection.execute(
                "UPDATE transport_legs SET status='cancelled',updated_at=? WHERE trip_id=? AND route_version=? "
                "AND status IN ('pending','in_transit','stopped')",
                (now, trip_id, trip["route_version"]),
            )
            repo.event("case", trip["case_id"], "trip.cancelled", payload["actor"],
                       {"trip_id": trip_id, "reason": payload["reason"]}, now)
            self._append_event(connection, trip_id, None, trip["route_version"], None,
                               "trip.cancelled", payload["actor"], occurred, reported,
                               payload["idempotency_key"], {"reason": payload["reason"]}, now)
            return self._assemble(repo, repo.trip(trip_id) or {})

    # ------------------------------------------------------------------ 改线

    def reroute(self, trip_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """保留原计划版本，未完成区段按新计划重建，并重新计算受影响预约。"""
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = MortuaryRepository(connection)
            trip = self._require_active_trip(repo, trip_id)
            if repo.transport_event_key(trip_id, "trip.rerouted", payload["idempotency_key"]):
                return self._assemble(repo, trip)
            old_version = int(trip["route_version"])
            old_legs = repo.legs(trip_id, old_version)
            new_version = old_version + 1
            plans = payload["legs"]
            old_plan_end = trip["planned_end_at"]
            new_start = to_storage(payload["planned_start_at"]) if payload.get("planned_start_at") else trip["planned_start_at"]
            new_end = to_storage(payload["planned_end_at"]) if payload.get("planned_end_at") else trip["planned_end_at"]
            if from_storage(new_end) <= from_storage(new_start):
                raise ValidationError("行程计划结束时间必须晚于开始时间")
            plans = payload["legs"]
            for index, old in enumerate(old_legs):
                if index >= len(plans):
                    if old["status"] not in ("pending", "cancelled"):
                        raise ConflictError("新路线不能删除已经发生的区段", context={"seq": index + 1})
                    continue
                plan = plans[index]
                if to_storage(plan["planned_start_at"]) < new_start or to_storage(plan["planned_end_at"]) > new_end:
                    raise ValidationError("区段计划窗口必须落在行程计划窗口内")
                if old["status"] not in ("pending", "cancelled"):
                    if plan["from_station"] != old["from_station"] or plan["to_station"] != old["to_station"]:
                        raise ConflictError("已经发车或确认的区段站点不能变更",
                                            context={"seq": index + 1, "status": old["status"]})
                    if plan["seal_code"] != old["seal_code"] or plan["vehicle_code"] != old["vehicle_code"]:
                        raise ConflictError("已经发生的区段车辆与封签不能直接修改，请使用换车事件",
                                            context={"seq": index + 1})
            # 保留旧计划快照后写入新版本
            self._insert_leg_rows(connection, trip_id, new_version, plans, now, old_legs=old_legs)
            self._snapshot_plan(connection, repo, trip_id, new_version, plans, payload["changed_by"],
                                payload["reason"], now)
            connection.execute(
                "UPDATE transport_trips SET route_version=?,planned_start_at=?,planned_end_at=?,updated_at=? WHERE id=?",
                (new_version, new_start, new_end, now, trip_id),
            )
            self._recompute_impacts(connection, repo, trip, new_version, old_plan_end, new_end, now)
            self._append_event(connection, trip_id, None, new_version, None, "trip.rerouted",
                               payload["changed_by"], now, now, payload["idempotency_key"],
                               {"old_version": old_version, "new_version": new_version,
                                "old_planned_end_at": old_plan_end, "new_planned_end_at": new_end,
                                "reason": payload["reason"]}, now)
            return self._assemble(repo, repo.trip(trip_id) or {})

    # ------------------------------------------------------------------ 内部

    def _do_depart(self, connection: sqlite3.Connection, repo: MortuaryRepository,
                   trip: dict[str, Any], leg: dict[str, Any], payload: dict[str, Any],
                   occurred: str, now: str) -> None:
        if leg["status"] != "pending":
            raise ConflictError("该区段已经发车，不能重复发车")
        if leg["seq"] > 1:
            previous = self._require_leg(repo, trip, leg["seq"] - 1)
            if previous["status"] != "confirmed":
                raise ConflictError("上一区段尚未由有权人员确认，下一段不能发车",
                                    context={"awaiting_seq": leg["seq"] - 1})
        connection.execute("UPDATE transport_legs SET status='in_transit',departed_at=?,updated_at=? WHERE id=?",
                           (occurred, now, leg["id"]))

    def _do_arrive(self, repo: MortuaryRepository, trip: dict[str, Any],
                   leg: dict[str, Any], payload: dict[str, Any], occurred: str) -> None:
        if leg["status"] == "arrived":
            raise ConflictError("该区段已回报到达")
        if leg["status"] != "in_transit":
            raise ConflictError("只有在途区段可以回报到达")
        if from_storage(occurred) < from_storage(leg["departed_at"]):
            raise ValidationError("到达时间不能早于发车时间")
        repo.connection.execute(
            "UPDATE transport_legs SET status='arrived',arrived_at=?,updated_at=? WHERE id=?",
            (occurred, self.now(), leg["id"]),
        )

    def _do_stop(self, repo: MortuaryRepository, trip: dict[str, Any],
                 leg: dict[str, Any], payload: dict[str, Any], occurred: str) -> None:
        if leg["status"] == "stopped":
            raise ConflictError("该区段已处于异常停留状态")
        if leg["status"] != "in_transit":
            raise ConflictError("只有在途区段可以上报异常停留")
        if from_storage(occurred) < from_storage(leg["departed_at"]):
            raise ValidationError("异常停留时间不能早于发车时间")
        repo.connection.execute("UPDATE transport_legs SET status='stopped',updated_at=? WHERE id=?",
                                (self.now(), leg["id"]))

    def _do_resume(self, repo: MortuaryRepository, trip: dict[str, Any],
                   leg: dict[str, Any], occurred: str) -> None:
        if leg["status"] != "stopped":
            raise ConflictError("只有异常停留中的区段可以恢复运输")
        repo.connection.execute("UPDATE transport_legs SET status='in_transit',updated_at=? WHERE id=?",
                                (self.now(), leg["id"]))

    def _require_active_trip(self, repo: MortuaryRepository, trip_id: int) -> dict[str, Any]:
        trip = repo.trip(trip_id)
        if trip is None:
            raise NotFoundError("运输行程不存在")
        if trip["status"] not in TRIP_ACTIVE:
            raise ConflictError("行程已结束，不能再记录事件", context={"status": trip["status"]})
        return trip

    def _require_leg(self, repo: MortuaryRepository, trip: dict[str, Any], seq: int) -> dict[str, Any]:
        if seq < 1:
            raise ValidationError("区段序号必须从 1 开始")
        legs = repo.legs(trip["id"], trip["route_version"])
        if seq > len(legs):
            raise NotFoundError("运输区段不存在")
        return legs[seq - 1]

    @staticmethod
    def _insert_leg_rows(connection: sqlite3.Connection, trip_id: int, version: int,
                         legs: list[dict[str, Any]], now: str,
                         old_legs: list[dict[str, Any]] | None = None) -> None:
        for index, plan in enumerate(legs, start=1):
            old = old_legs[index - 1] if old_legs and index - 1 < len(old_legs) else None
            live = old is not None and old["status"] not in ("pending", "cancelled")
            if live:
                # 已实际发生的区段携带真实车辆/封签与状态进入新版本
                vehicle, carrier, seal = old["vehicle_code"], old["carrier"], old["seal_code"]
                status, departed, arrived = old["status"], old["departed_at"], old["arrived_at"]
                confirmed_by, role, observed = old["confirmed_by"], old["confirmer_role"], old["observed_seal_code"]
                condition, confirmed_at, rejected_at = old["condition_note"], old["confirmed_at"], old["rejected_at"]
            else:
                vehicle, carrier, seal = plan["vehicle_code"], plan.get("carrier", ""), plan["seal_code"]
                status = "pending"
                departed = arrived = confirmed_at = rejected_at = None
                confirmed_by = role = observed = condition = ""
            connection.execute(
                "INSERT INTO transport_legs(trip_id,route_version,seq,from_station,to_station,vehicle_code,carrier,"
                "seal_code,planned_start_at,planned_end_at,confirm_roles_json,status,departed_at,arrived_at,"
                "confirmed_by,confirmer_role,observed_seal_code,condition_note,confirmed_at,rejected_at,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (trip_id, version, index, plan["from_station"], plan["to_station"], vehicle, carrier, seal,
                 to_storage(plan["planned_start_at"]), to_storage(plan["planned_end_at"]),
                 json.dumps(plan.get("confirm_roles", []), ensure_ascii=False), status, departed, arrived,
                 confirmed_by, role, observed, condition, confirmed_at, rejected_at, now, now),
            )

    @staticmethod
    def _snapshot_plan(connection: sqlite3.Connection, repo: MortuaryRepository, trip_id: int, version: int,
                       legs: list[dict[str, Any]], changed_by: str, reason: str, now: str) -> None:
        snapshot = [
            {
                "seq": index,
                "from_station": plan["from_station"],
                "to_station": plan["to_station"],
                "vehicle_code": plan["vehicle_code"],
                "carrier": plan.get("carrier", ""),
                "seal_code": plan["seal_code"],
                "planned_start_at": to_storage(plan["planned_start_at"]),
                "planned_end_at": to_storage(plan["planned_end_at"]),
                "confirm_roles": plan.get("confirm_roles", []),
            }
            for index, plan in enumerate(legs, start=1)
        ]
        connection.execute(
            "INSERT INTO transport_route_versions(trip_id,version,reason,legs_json,changed_by,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (trip_id, version, reason, json.dumps(snapshot, ensure_ascii=False, sort_keys=True), changed_by, now),
        )

    def _recompute_impacts(self, connection: sqlite3.Connection, repo: MortuaryRepository,
                           trip: dict[str, Any], version: int, old_plan_end: str,
                           new_plan_end: str, now: str) -> None:
        reservations = repo.trip_reservations(trip["case_id"])
        for reservation in reservations:
            if from_storage(new_plan_end) <= from_storage(reservation["start_at"]):
                continue  # 新计划能在预约开始前抵达，不受影响
            late_seconds = int((from_storage(new_plan_end) - from_storage(reservation["start_at"])).total_seconds())
            detail = (
                f"计划抵达 {new_plan_end} 晚于资源 {reservation['resource_code']} 预约开始 "
                f"{reservation['start_at']}，约延误 {max(late_seconds, 0) // 60} 分钟；"
                f"原计划抵达 {old_plan_end}"
            )
            connection.execute(
                "INSERT INTO transport_impacts(trip_id,route_version,reservation_id,resource_code,start_at,end_at,"
                "detail,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (trip["id"], version, reservation["id"], reservation["resource_code"],
                 reservation["start_at"], reservation["end_at"], detail, now),
            )

    @staticmethod
    def _append_event(connection: sqlite3.Connection, trip_id: int, leg_id: int | None, version: int,
                      seq: int | None, event_type: str, actor: str, occurred_at: str, reported_at: str,
                      key: str, payload: dict[str, Any], now: str) -> None:
        connection.execute(
            "INSERT INTO transport_events(trip_id,leg_id,route_version,seq,event_type,actor,occurred_at,"
            "reported_at,idempotency_key,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (trip_id, leg_id, version, seq, event_type, actor, occurred_at, reported_at, key,
             json.dumps(payload, ensure_ascii=False, sort_keys=True), now),
        )

    def _assemble(self, repo: MortuaryRepository, trip: dict[str, Any]) -> dict[str, Any]:
        trip_id = int(trip["id"])
        version = int(trip["route_version"])
        legs = repo.legs(trip_id, version)
        events = repo.transport_events(trip_id)
        now_dt = from_storage(self.now())
        assembled_legs: list[dict[str, Any]] = []
        events_by_seq: dict[int, list[dict[str, Any]]] = {}
        for event in events:
            if event["seq"] is not None:
                events_by_seq.setdefault(event["seq"], []).append(event)
        for leg in legs:
            item = dict(leg)
            item["confirm_roles"] = json.loads(item.pop("confirm_roles_json"))
            item["events"] = events_by_seq.get(leg["seq"], [])
            planned_end = from_storage(leg["planned_end_at"])
            item["minutes_late"] = None
            if leg["status"] in ("confirmed", "arrived", "rejected") and leg["arrived_at"]:
                delta = (from_storage(leg["arrived_at"]) - planned_end).total_seconds() // 60
                item["minutes_late"] = int(delta) if delta > 0 else 0
            elif leg["status"] in ("pending", "in_transit", "stopped"):
                delta = (now_dt - planned_end).total_seconds() // 60
                if delta > 0:
                    item["minutes_late"] = int(delta)
            item["waiting_minutes"] = None
            if leg["status"] == "arrived" and leg["arrived_at"]:
                item["waiting_minutes"] = int((now_dt - from_storage(leg["arrived_at"])).total_seconds() // 60)
            assembled_legs.append(item)
        active_leg = next((leg for leg in assembled_legs if leg["status"] in ("in_transit", "stopped", "arrived", "rejected")), None)
        if active_leg is None:
            active_leg = next((leg for leg in assembled_legs if leg["status"] == "pending"), None)
        if active_leg is None and assembled_legs:
            active_leg = assembled_legs[-1]
        result = dict(trip)
        result["legs"] = assembled_legs
        result["events"] = events
        result["route_versions"] = repo.route_versions(trip_id)
        result["impacts"] = repo.impacts(trip_id)
        result["current_carrier"] = None
        if trip["status"] in TRIP_ACTIVE and active_leg is not None:
            result["current_carrier"] = {
                "seq": active_leg["seq"], "vehicle_code": active_leg["vehicle_code"],
                "carrier": active_leg["carrier"], "seal_code": active_leg["seal_code"],
                "leg_status": active_leg["status"],
            }
        result["overdue_legs"] = [
            {"seq": leg["seq"], "vehicle_code": leg["vehicle_code"], "carrier": leg["carrier"],
             "status": leg["status"], "minutes_late": leg["minutes_late"]}
            for leg in assembled_legs if leg["minutes_late"] is not None and leg["status"] != "cancelled"
        ]
        result["pending_confirmations"] = [
            {"seq": leg["seq"], "to_station": leg["to_station"], "vehicle_code": leg["vehicle_code"],
             "carrier": leg["carrier"], "arrived_at": leg["arrived_at"]}
            for leg in assembled_legs if leg["status"] == "arrived"
        ]
        result["responsibility_chain"] = self._chain(assembled_legs)
        return result

    @staticmethod
    def _chain(legs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        chain = []
        for leg in legs:
            chain.append({
                "seq": leg["seq"],
                "from_station": leg["from_station"],
                "to_station": leg["to_station"],
                "vehicle_code": leg["vehicle_code"],
                "carrier": leg["carrier"],
                "seal_code": leg["seal_code"],
                "status": leg["status"],
                "planned_window": [leg["planned_start_at"], leg["planned_end_at"]],
                "departed_at": leg["departed_at"],
                "arrived_at": leg["arrived_at"],
                "confirmed_by": leg["confirmed_by"],
                "confirmer_role": leg["confirmer_role"],
                "confirmed_at": leg["confirmed_at"],
                "events": [
                    {"event_type": event["event_type"], "actor": event["actor"],
                     "occurred_at": event["occurred_at"], "reported_at": event["reported_at"],
                     "payload": event["payload"]}
                    for event in leg["events"]
                ],
            })
        return chain
