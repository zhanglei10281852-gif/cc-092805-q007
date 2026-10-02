from __future__ import annotations

from datetime import UTC, datetime

from app.core.clock import FrozenClock
from app.database import get_connection
from app.mortuary.transport import TransportService


def _create_case(client, ref: str = "TRIP-001") -> dict:
    response = client.post("/api/mortuary/cases?actor=intake-clerk", json={
        "external_ref": ref, "decedent_name": "张德安", "identity_number": "ID-440100-1938",
        "death_time": "2026-09-30T08:30:00Z", "received_from": "市第二医院",
        "family_contact": "张明", "family_phone": "13800000000", "special_notes": "",
    })
    assert response.status_code == 201, response.text
    return response.json()


def _trip_payload(case_id: int, key: str = "trip-key-001") -> dict:
    return {
        "case_id": case_id, "created_by": "coordinator-zhao", "idempotency_key": key,
        "planned_start_at": "2026-10-02T01:00:00Z", "planned_end_at": "2026-10-02T06:00:00Z",
        "legs": [
            {"from_station": "市第二医院太平间", "to_station": "县级殡仪站", "vehicle_code": "沪A-1001",
             "carrier": "城运一队", "seal_code": "SEAL-L1", "confirm_roles": ["station_keeper"],
             "planned_start_at": "2026-10-02T01:00:00Z", "planned_end_at": "2026-10-02T03:00:00Z"},
            {"from_station": "县级殡仪站", "to_station": "市馆交接区", "vehicle_code": "沪B-2002",
             "carrier": "城际联运", "seal_code": "SEAL-L2", "confirm_roles": ["city_handler"],
             "planned_start_at": "2026-10-02T03:30:00Z", "planned_end_at": "2026-10-02T05:00:00Z"},
            {"from_station": "市馆交接区", "to_station": "市馆冷藏室", "vehicle_code": "沪C-3003",
             "carrier": "市馆车队", "seal_code": "SEAL-L3", "confirm_roles": ["cold_keeper"],
             "planned_start_at": "2026-10-02T05:15:00Z", "planned_end_at": "2026-10-02T06:00:00Z"},
        ],
    }


def _create_trip(client, case_id: int, key: str = "trip-key-001") -> dict:
    response = client.post("/api/mortuary/transport-trips", json=_trip_payload(case_id, key))
    assert response.status_code == 201, response.text
    return response.json()


def test_trip_is_ordered_legs_with_windows_and_seals(client):
    case = _create_case(client)
    trip = _create_trip(client, case["id"])
    assert [leg["seq"] for leg in trip["legs"]] == [1, 2, 3]
    assert trip["status"] == "planned"
    assert trip["route_version"] == 1
    assert [leg["seal_code"] for leg in trip["legs"]] == ["SEAL-L1", "SEAL-L2", "SEAL-L3"]
    # 链不闭合应被拒绝
    bad = _trip_payload(case["id"], "trip-bad-0001")
    bad["legs"][1]["from_station"] = "别的站点"
    response = client.post("/api/mortuary/transport-trips", json=bad)
    assert response.status_code == 422


def test_next_segment_cannot_start_before_confirmation(client):
    case = _create_case(client)
    trip = _create_trip(client, case["id"])
    # 第一段发车后，第二段不能发车
    departed = client.post(f"/api/mortuary/transport-trips/{trip['id']}/legs/1/events/departed", json={
        "actor": "driver-li", "idempotency_key": "depart-001", "occurred_at": "2026-10-02T01:10:00Z"})
    assert departed.status_code == 200, departed.text
    blocked = client.post(f"/api/mortuary/transport-trips/{trip['id']}/legs/2/events/departed", json={
        "actor": "driver-wang", "idempotency_key": "depart-002", "occurred_at": "2026-10-02T03:35:00Z"})
    assert blocked.status_code == 409
    assert blocked.json()["error"]["context"]["awaiting_seq"] == 1
    # 无权限角色不能确认
    client.post(f"/api/mortuary/transport-trips/{trip['id']}/legs/1/events/arrived", json={
        "actor": "driver-li", "idempotency_key": "arrive-001", "occurred_at": "2026-10-02T02:50:00Z"})
    forbidden = client.post(f"/api/mortuary/transport-trips/{trip['id']}/legs/1/confirm", json={
        "confirmed_by": "someone", "role": "guest", "observed_seal_code": "SEAL-L1",
        "idempotency_key": "confirm-001"})
    assert forbidden.status_code == 403
    # 封签不符被拒收
    mismatch = client.post(f"/api/mortuary/transport-trips/{trip['id']}/legs/1/confirm", json={
        "confirmed_by": "keeper-chen", "role": "station_keeper", "observed_seal_code": "SEAL-WRONG",
        "idempotency_key": "confirm-002"})
    assert mismatch.status_code == 409
    detail = client.get(f"/api/mortuary/transport-trips/{trip['id']}").json()
    assert detail["legs"][0]["status"] == "rejected"


def test_full_happy_path_completes_trip_and_updates_case(client):
    case = _create_case(client, "TRIP-HAPPY")
    trip = _create_trip(client, case["id"], "trip-happy-001")
    flow = [
        (1, "01:10", "02:50", "station_keeper", "keeper-chen"),
        (2, "03:40", "04:55", "city_handler", "handler-sun"),
        (3, "05:20", "05:50", "cold_keeper", "keeper-zhou"),
    ]
    for seq, dep, arr, role, confirmer in flow:
        assert client.post(f"/api/mortuary/transport-trips/{trip['id']}/legs/{seq}/events/departed", json={
            "actor": f"driver-{seq}", "idempotency_key": f"depart-00{seq}",
            "occurred_at": f"2026-10-02T{dep}:00Z"}).status_code == 200
        assert client.post(f"/api/mortuary/transport-trips/{trip['id']}/legs/{seq}/events/arrived", json={
            "actor": f"driver-{seq}", "idempotency_key": f"arrive-00{seq}",
            "occurred_at": f"2026-10-02T{arr}:00Z"}).status_code == 200
        confirmed = client.post(f"/api/mortuary/transport-trips/{trip['id']}/legs/{seq}/confirm", json={
            "confirmed_by": confirmer, "role": role, "observed_seal_code": f"SEAL-L{seq}",
            "condition_note": "封签完整", "idempotency_key": f"confirm-00{seq}"})
        assert confirmed.status_code == 200, confirmed.text
    detail = client.get(f"/api/mortuary/transport-trips/{trip['id']}").json()
    assert detail["status"] == "completed"
    assert detail["current_carrier"] is None
    case_detail = client.get(f"/api/mortuary/cases/{case['id']}").json()
    assert case_detail["current_location"] == "市馆冷藏室"
    assert case_detail["status"] == "in_custody"


def test_events_are_immutable_and_duplicate_reports_do_not_advance_twice(client):
    case = _create_case(client, "TRIP-DUP")
    trip = _create_trip(client, case["id"], "trip-dup-0001")
    depart = {"actor": "driver-li", "idempotency_key": "depart-dup", "occurred_at": "2026-10-02T01:10:00Z"}
    first = client.post(f"/api/mortuary/transport-trips/{trip['id']}/legs/1/events/departed", json=depart)
    second = client.post(f"/api/mortuary/transport-trips/{trip['id']}/legs/1/events/departed", json=depart)
    assert first.status_code == 200 and second.status_code == 200
    detail = client.get(f"/api/mortuary/transport-trips/{trip['id']}").json()
    departed_events = [e for e in detail["events"] if e["event_type"] == "leg.departed"]
    assert len(departed_events) == 1  # 重复回报没有产生第二条事件
    # 再次发车仍然被拒绝，状态只推进了一次
    again = client.post(f"/api/mortuary/transport-trips/{trip['id']}/legs/1/events/departed",
                        json={**depart, "idempotency_key": "dep-again"})
    assert again.status_code == 409


def test_late_events_keep_occurred_and_reported_times(client):
    case = _create_case(client, "TRIP-LATE")
    trip = _create_trip(client, case["id"], "trip-late-0001")
    client.post(f"/api/mortuary/transport-trips/{trip['id']}/legs/1/events/departed", json={
        "actor": "driver-li", "idempotency_key": "dep-late",
        "occurred_at": "2026-10-02T01:10:00Z", "reported_at": "2026-10-02T01:12:00Z"})
    # 实际 04:30 才到（计划 03:00），05:00 才补报——两个时间都要留存
    arrived = client.post(f"/api/mortuary/transport-trips/{trip['id']}/legs/1/events/arrived", json={
        "actor": "driver-li", "idempotency_key": "arr-late",
        "occurred_at": "2026-10-02T04:30:00Z", "reported_at": "2026-10-02T05:00:00Z",
        "note": "高速封路绕行"})
    assert arrived.status_code == 200
    detail = client.get(f"/api/mortuary/transport-trips/{trip['id']}").json()
    event = next(e for e in detail["events"] if e["event_type"] == "leg.arrived")
    assert event["occurred_at"] == "2026-10-02T04:30:00+00:00"
    assert event["reported_at"] == "2026-10-02T05:00:00+00:00"
    assert detail["legs"][0]["minutes_late"] == 90
    # 报送早于发生应被拒绝
    bad = client.post(f"/api/mortuary/transport-trips/{trip['id']}/legs/1/events/abnormal_stop", json={
        "actor": "driver-li", "idempotency_key": "stop-bad",
        "occurred_at": "2026-10-02T06:00:00Z", "reported_at": "2026-10-02T05:00:00Z"})
    assert bad.status_code == 422


def test_abnormal_stop_resume_and_vehicle_change_preserve_responsibility(client):
    case = _create_case(client, "TRIP-VEH")
    trip = _create_trip(client, case["id"], "trip-veh-0001")
    client.post(f"/api/mortuary/transport-trips/{trip['id']}/legs/1/events/departed", json={
        "actor": "driver-li", "idempotency_key": "depart-veh", "occurred_at": "2026-10-02T01:10:00Z"})
    stopped = client.post(f"/api/mortuary/transport-trips/{trip['id']}/legs/1/events/abnormal_stop", json={
        "actor": "driver-li", "idempotency_key": "stop-veh", "occurred_at": "2026-10-02T02:00:00Z",
        "reason": "车辆故障"})
    assert stopped.status_code == 200
    # 县级殡仪站换车并换封签
    changed = client.post(f"/api/mortuary/transport-trips/{trip['id']}/legs/1/vehicle-change", json={
        "actor": "coordinator-zhao", "idempotency_key": "change-veh",
        "occurred_at": "2026-10-02T02:40:00Z", "new_vehicle_code": "沪A-1099",
        "new_carrier": "城运一队-备班", "new_seal_code": "SEAL-L1-R", "reason": "原车故障拖修"})
    assert changed.status_code == 200, changed.text
    resumed = client.post(f"/api/mortuary/transport-trips/{trip['id']}/legs/1/events/resumed", json={
        "actor": "driver-gao", "idempotency_key": "resume-veh", "occurred_at": "2026-10-02T02:50:00Z"})
    assert resumed.status_code == 200
    detail = client.get(f"/api/mortuary/transport-trips/{trip['id']}").json()
    assert detail["legs"][0]["vehicle_code"] == "沪A-1099"
    assert detail["legs"][0]["seal_code"] == "SEAL-L1-R"
    change_event = next(e for e in detail["events"] if e["event_type"] == "leg.vehicle_changed")
    assert change_event["payload"]["old_vehicle_code"] == "沪A-1001"
    assert change_event["payload"]["old_seal_code"] == "SEAL-L1"
    assert change_event["payload"]["new_seal_code"] == "SEAL-L1-R"
    # 责任链同时保留原承运与现承运
    chain = detail["responsibility_chain"][0]
    assert chain["vehicle_code"] == "沪A-1099"
    types = [e["event_type"] for e in chain["events"]]
    assert types == ["leg.departed", "leg.abnormal_stop", "leg.vehicle_changed", "leg.resumed"]


def test_reroute_keeps_original_plan_and_recomputes_affected_reservations(client):
    case = _create_case(client, "TRIP-ROUTE")
    # 预约在原计划 06:00 之后、新计划 09:00 之前 -> 受改线影响
    assert client.post("/api/mortuary/resources?actor=scheduler", json={
        "code": "COLD-1", "name": "冷藏室1号位", "kind": "cold_storage",
        "site_code": "SITE-1", "capacity": 1, "attributes": {}}).status_code == 201
    assert client.post("/api/mortuary/reservations", json={
        "resource_code": "COLD-1", "case_id": case["id"],
        "start_at": "2026-10-02T07:00:00Z", "end_at": "2026-10-02T08:00:00Z",
        "purpose": "入库冷藏", "created_by": "scheduler",
        "idempotency_key": "cold-res-0001"}).status_code == 201
    trip = _create_trip(client, case["id"], "trip-route-0001")
    # 第一段完成后改线：后两段绕行，整体推迟到 09:00
    client.post(f"/api/mortuary/transport-trips/{trip['id']}/legs/1/events/departed", json={
        "actor": "driver-li", "idempotency_key": "depart-r01", "occurred_at": "2026-10-02T01:10:00Z"})
    client.post(f"/api/mortuary/transport-trips/{trip['id']}/legs/1/events/arrived", json={
        "actor": "driver-li", "idempotency_key": "arrive-r01", "occurred_at": "2026-10-02T02:50:00Z"})
    client.post(f"/api/mortuary/transport-trips/{trip['id']}/legs/1/confirm", json={
        "confirmed_by": "keeper-chen", "role": "station_keeper",
        "observed_seal_code": "SEAL-L1", "idempotency_key": "confirm-r01"})
    rerouted = client.post(f"/api/mortuary/transport-trips/{trip['id']}/reroute", json={
        "changed_by": "coordinator-zhao", "reason": "国道施工绕行南站",
        "idempotency_key": "reroute-001", "planned_end_at": "2026-10-02T09:00:00Z",
        "legs": [
            {"from_station": "市第二医院太平间", "to_station": "县级殡仪站", "vehicle_code": "沪A-1001",
             "carrier": "城运一队", "seal_code": "SEAL-L1",
             "planned_start_at": "2026-10-02T01:00:00Z", "planned_end_at": "2026-10-02T03:00:00Z"},
            {"from_station": "县级殡仪站", "to_station": "南站中转", "vehicle_code": "沪D-4004",
             "carrier": "城际联运", "seal_code": "SEAL-L2B",
             "planned_start_at": "2026-10-02T03:40:00Z", "planned_end_at": "2026-10-02T06:30:00Z"},
            {"from_station": "南站中转", "to_station": "市馆交接区", "vehicle_code": "沪D-4005",
             "carrier": "城际联运", "seal_code": "SEAL-L2C",
             "planned_start_at": "2026-10-02T06:40:00Z", "planned_end_at": "2026-10-02T08:00:00Z"},
            {"from_station": "市馆交接区", "to_station": "市馆冷藏室", "vehicle_code": "沪C-3003",
             "carrier": "市馆车队", "seal_code": "SEAL-L3",
             "planned_start_at": "2026-10-02T08:10:00Z", "planned_end_at": "2026-10-02T09:00:00Z"},
        ]})
    assert rerouted.status_code == 200, rerouted.text
    detail = client.get(f"/api/mortuary/transport-trips/{trip['id']}").json()
    assert detail["route_version"] == 2
    assert len(detail["legs"]) == 4
    # 已完成的第一段随真实状态进入新版本，后续行程从第二段继续
    assert detail["legs"][0]["status"] == "confirmed"
    assert [leg["status"] for leg in detail["legs"][1:]] == ["pending", "pending", "pending"]
    # 原计划完整保留
    versions = detail["route_versions"]
    assert [v["version"] for v in versions] == [1, 2]
    assert versions[0]["reason"] == "初始计划"
    assert len(versions[0]["legs"]) == 3 and len(versions[1]["legs"]) == 4
    # 受影响预约已重算
    assert len(detail["impacts"]) == 1
    assert detail["impacts"][0]["resource_code"] == "COLD-1"
    assert "延误" in detail["impacts"][0]["detail"]
    # 改线后可以继续走新路线
    assert client.post(f"/api/mortuary/transport-trips/{trip['id']}/legs/2/events/departed", json={
        "actor": "driver-feng", "idempotency_key": "depart-r02",
        "occurred_at": "2026-10-02T03:45:00Z"}).status_code == 200


def test_cancel_records_immutable_event_and_blocks_further_progress(client):
    case = _create_case(client, "TRIP-CANCEL")
    trip = _create_trip(client, case["id"], "trip-cancel-0001")
    cancelled = client.post(f"/api/mortuary/transport-trips/{trip['id']}/cancel", json={
        "actor": "coordinator-zhao", "idempotency_key": "cancel-001",
        "occurred_at": "2026-10-02T01:30:00Z", "reported_at": "2026-10-02T01:35:00Z",
        "reason": "家属暂停治丧"})
    assert cancelled.status_code == 200
    detail = client.get(f"/api/mortuary/transport-trips/{trip['id']}").json()
    assert detail["status"] == "cancelled"
    assert all(leg["status"] == "cancelled" for leg in detail["legs"])
    blocked = client.post(f"/api/mortuary/transport-trips/{trip['id']}/legs/1/events/departed", json={
        "actor": "driver-li", "idempotency_key": "dep-after-cancel",
        "occurred_at": "2026-10-02T02:00:00Z"})
    assert blocked.status_code == 409


def test_overview_shows_carrier_overdue_and_pending_nodes(client):
    case = _create_case(client, "TRIP-VIEW")
    trip = _create_trip(client, case["id"], "trip-view-0001")
    client.post(f"/api/mortuary/transport-trips/{trip['id']}/legs/1/events/departed", json={
        "actor": "driver-li", "idempotency_key": "dep-view", "occurred_at": "2026-10-02T01:10:00Z"})
    client.post(f"/api/mortuary/transport-trips/{trip['id']}/legs/1/events/arrived", json={
        "actor": "driver-li", "idempotency_key": "arr-view", "occurred_at": "2026-10-02T04:40:00Z"})
    overview = client.get("/api/mortuary/transport-trips/overview").json()
    active = next(t for t in overview["active_trips"] if t["trip_id"] == trip["id"])
    assert active["current_carrier"]["vehicle_code"] == "沪A-1001"
    assert active["current_carrier"]["carrier"] == "城运一队"
    pending = next(p for p in overview["pending_confirmations"] if p["trip_id"] == trip["id"])
    assert pending["seq"] == 1 and pending["to_station"] == "县级殡仪站"
    overdue = next(o for o in overview["overdue_legs"] if o["trip_id"] == trip["id"] and o["seq"] == 1)
    assert overdue["minutes_late"] >= 100


def test_trip_resumes_after_service_restart(client):
    case = _create_case(client, "TRIP-RESTART")
    trip = _create_trip(client, case["id"], "trip-restart-0001")
    client.post(f"/api/mortuary/transport-trips/{trip['id']}/legs/1/events/departed", json={
        "actor": "driver-li", "idempotency_key": "dep-restart",
        "occurred_at": "2026-10-02T01:10:00Z"})
    # 模拟服务重启：用全新的服务实例与时钟继续未完成行程
    clock = FrozenClock(datetime(2026, 10, 2, 5, 0, tzinfo=UTC))
    service = TransportService(get_connection(), clock)
    detail = service.get_trip(trip["id"])
    assert detail["legs"][0]["status"] == "in_transit"
    arrived = service.report_event(trip["id"], 1, "arrived", {
        "actor": "driver-li", "idempotency_key": "arr-restart",
        "occurred_at": datetime(2026, 10, 2, 2, 50, tzinfo=UTC)})
    assert arrived["legs"][0]["status"] == "arrived"
    confirmed = service.confirm_leg(trip["id"], 1, {
        "confirmed_by": "keeper-chen", "role": "station_keeper",
        "observed_seal_code": "SEAL-L1", "condition_note": "",
        "idempotency_key": "conf-restart"})
    assert confirmed["legs"][0]["status"] == "confirmed"
    assert confirmed["legs"][1]["status"] == "pending"
