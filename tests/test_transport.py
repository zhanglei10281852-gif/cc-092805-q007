from __future__ import annotations

import sqlite3


def create_case(client, ref: str = "TRIP-CASE-001") -> dict:
    response = client.post(
        "/api/mortuary/cases?actor=intake-clerk",
        json={"external_ref": ref, "decedent_name": "张长河", "identity_number": "ID-440100-1942",
              "death_time": "2026-09-15T06:00:00Z", "received_from": "市第二医院",
              "family_contact": "张明", "family_phone": "13800000000", "special_notes": ""},
    )
    assert response.status_code == 201, response.text
    return response.json()


def two_segment_trip(client, case_id: int, ref: str = "TRIP-001", key: str = "trip-create-001") -> dict:
    payload = {
        "external_ref": ref, "case_id": case_id, "created_by": "dispatcher-lin", "idempotency_key": key,
        "segments": [
            {"from_station": "市第二医院太平间", "to_station": "县级殡仪站", "carrier": "安顺接运队",
             "vehicle_code": "VAN-01", "driver": "老赵", "planned_depart_at": "2026-09-15T08:00:00Z",
             "planned_arrive_at": "2026-09-15T09:00:00Z", "seal_code": "SEAL-A", "confirmer": "station-master-qian"},
            {"from_station": "县级殡仪站", "to_station": "市馆冷藏室", "carrier": "市级联运公司",
             "vehicle_code": "VAN-02", "driver": "老钱", "planned_depart_at": "2026-09-15T09:30:00Z",
             "planned_arrive_at": "2026-09-15T10:30:00Z", "seal_code": "SEAL-B", "confirmer": "keeper-wang"},
        ],
    }
    response = client.post("/api/mortuary/transport-trips", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_full_trip_with_gate_events_seal_change_and_chain(client):
    case = create_case(client)
    trip = two_segment_trip(client, case["id"])

    # 上一段未确认，下一段不能发车
    blocked = client.post(f"/api/mortuary/transport-trips/{trip['id']}/segments/1/departure",
                          json={"actor": "driver-qian", "idempotency_key": "dep-seg-10", "occurred_at": "2026-09-15T09:35:00Z"})
    assert blocked.status_code == 409

    # 第一段发车
    dep0 = client.post(f"/api/mortuary/transport-trips/{trip['id']}/segments/0/departure",
                       json={"actor": "老赵", "idempotency_key": "dep-seg-00", "occurred_at": "2026-09-15T08:05:00Z", "seal_code": "SEAL-A"})
    assert dep0.status_code == 200, dep0.text
    assert dep0.json()["status"] == "in_progress"
    assert dep0.json()["current_carrier"] == "安顺接运队"

    # 第一段晚点到站
    arr0 = client.post(f"/api/mortuary/transport-trips/{trip['id']}/segments/0/arrival",
                       json={"actor": "老赵", "idempotency_key": "arr-seg-00", "occurred_at": "2026-09-15T09:10:00Z",
                             "observed_seal_code": "SEAL-A", "condition_note": "封签完整"})
    assert arr0.status_code == 200

    # 重复回报不得推进两次：事件不增加、分段不重复计时
    events_before = len(client.get(f"/api/mortuary/transport-trips/{trip['id']}").json()["events"])
    repeated = client.post(f"/api/mortuary/transport-trips/{trip['id']}/segments/0/arrival",
                           json={"actor": "老赵", "idempotency_key": "arr-seg-00", "occurred_at": "2026-09-15T09:10:00Z",
                                 "observed_seal_code": "SEAL-A"})
    assert repeated.status_code == 200
    detail = client.get(f"/api/mortuary/transport-trips/{trip['id']}").json()
    assert len(detail["events"]) == events_before
    assert detail["segments"][0]["status"] == "arrived"

    # 迟到报送：发生时间早、报送时间晚，两个时间都保留
    stop = client.post(f"/api/mortuary/transport-trips/{trip['id']}/segments/0/abnormal-stops",
                       json={"actor": "老赵", "idempotency_key": "stop-seg-00", "occurred_at": "2026-09-15T08:20:00Z",
                             "location": "312国道K88", "reason": "临时交通管制", "note": "等候放行"})
    assert stop.status_code == 200
    stop_event = [event for event in client.get(f"/api/mortuary/transport-trips/{trip['id']}").json()["events"]
                  if event["event_type"] == "segment.abnormal_stop"][0]
    assert stop_event["occurred_at"] == "2026-09-15T08:20:00+00:00"
    assert stop_event["reported_at"] > stop_event["occurred_at"]
    assert stop_event["payload"]["late_minutes"] >= 0

    # 非指定确认人无权确认
    forbidden = client.post(f"/api/mortuary/transport-trips/{trip['id']}/segments/0/confirm",
                            json={"confirmed_by": "intruder", "idempotency_key": "conf-seg-0b",
                                  "observed_seal_code": "SEAL-A"})
    assert forbidden.status_code == 403

    # 有权人员确认第一段
    confirmed0 = client.post(f"/api/mortuary/transport-trips/{trip['id']}/segments/0/confirm",
                             json={"confirmed_by": "station-master-qian", "idempotency_key": "conf-seg-00",
                                   "observed_seal_code": "SEAL-A", "condition_note": "县站接收无误"})
    assert confirmed0.status_code == 200
    assert confirmed0.json()["segments"][0]["status"] == "confirmed"

    # 第二段发车、迟到报送（发生时间与报送时间并存）
    assert client.post(f"/api/mortuary/transport-trips/{trip['id']}/segments/1/departure",
                       json={"actor": "老钱", "idempotency_key": "dep-seg-01", "occurred_at": "2026-09-15T09:40:00Z"}).status_code == 200
    delay = client.post(f"/api/mortuary/transport-trips/{trip['id']}/segments/1/delays",
                        json={"actor": "老钱", "idempotency_key": "delay-seg-01", "occurred_at": "2026-09-15T09:50:00Z",
                              "delay_minutes": 30, "reason": "高速拥堵"})
    assert delay.status_code == 200
    delay_event = [event for event in delay.json()["events"] if event["event_type"] == "segment.delayed"][0]
    assert delay_event["occurred_at"] != delay_event["reported_at"]
    assert delay_event["payload"]["delay_minutes"] == 30

    # 中途换车换封签：承运方变更留痕
    changed = client.post(f"/api/mortuary/transport-trips/{trip['id']}/segments/1/vehicle-changes",
                          json={"actor": "dispatcher-lin", "idempotency_key": "swap-seg-01", "occurred_at": "2026-09-15T10:00:00Z",
                                "new_carrier": "市级应急车队", "new_vehicle_code": "VAN-09", "new_driver": "老孙",
                                "new_seal_code": "SEAL-B2", "reason": "原车故障"})
    assert changed.status_code == 200
    assert changed.json()["current_carrier"] == "市级应急车队"

    # 旧封签到站必须被拒，但封签不符事件不可覆盖地落账
    bad_arrival = client.post(f"/api/mortuary/transport-trips/{trip['id']}/segments/1/arrival",
                              json={"actor": "老孙", "idempotency_key": "arr-seg-1b", "occurred_at": "2026-09-15T10:40:00Z",
                                    "observed_seal_code": "SEAL-B"})
    assert bad_arrival.status_code == 409
    detail = client.get(f"/api/mortuary/transport-trips/{trip['id']}").json()
    assert detail["segments"][1]["status"] == "in_progress"
    assert any(event["event_type"] == "segment.seal_mismatch" for event in detail["events"])

    # 持新封签正常到站并由指定人员确认，行程完成
    assert client.post(f"/api/mortuary/transport-trips/{trip['id']}/segments/1/arrival",
                       json={"actor": "老孙", "idempotency_key": "arr-seg-01", "occurred_at": "2026-09-15T10:40:00Z",
                             "observed_seal_code": "SEAL-B2"}).status_code == 200
    finish = client.post(f"/api/mortuary/transport-trips/{trip['id']}/segments/1/confirm",
                         json={"confirmed_by": "keeper-wang", "idempotency_key": "conf-seg-01",
                               "observed_seal_code": "SEAL-B2"})
    assert finish.status_code == 200
    assert finish.json()["status"] == "completed"
    assert finish.json()["current_location"] == "市馆冷藏室"

    final = client.get(f"/api/mortuary/transport-trips/{trip['id']}").json()
    case_detail = client.get(f"/api/mortuary/cases/{case['id']}").json()
    assert case_detail["status"] == "in_custody"
    assert case_detail["current_location"] == "市馆冷藏室"

    # 完整责任链按发生时间排序，承运方、站点随事件带出
    chain = final["responsibility_chain"]
    types = [node["event_type"] for node in chain]
    assert types.index("segment.abnormal_stop") < types.index("segment.arrived")
    swap_node = next(node for node in chain if node["event_type"] == "segment.vehicle_changed")
    assert swap_node["payload"]["previous_carrier"] == "市级联运公司"
    assert swap_node["payload"]["carrier"] == "市级应急车队"
    assert swap_node["payload"]["previous_seal_code"] == "SEAL-B"

    # 超时段可归责到具体分段与承运方
    overtime_segments = {item["sequence_index"]: item for item in final["overtimes"]}
    assert overtime_segments[0]["arrival_overdue_seconds"] == 600
    assert overtime_segments[1]["departure_overdue_seconds"] == 600
    assert overtime_segments[1]["reported_delay_minutes"] == 30

    # 事件不可覆盖、不可删除：账本只追加，顺序与 API 事件一致
    stored = [row[0] for row in _open_connection(client).execute(
        "SELECT event_type FROM transport_events WHERE trip_id=? ORDER BY id", (trip["id"],))]
    assert stored == [event["event_type"] for event in final["events"]]
    # 责任链按发生时间排序
    occurred = [node["occurred_at"] for node in chain]
    assert occurred == sorted(occurred)


def test_coordination_shows_pending_nodes_and_overtimes(client):
    case = create_case(client, "TRIP-CASE-002")
    trip = two_segment_trip(client, case["id"], ref="TRIP-002", key="trip-create-002")
    client.post(f"/api/mortuary/transport-trips/{trip['id']}/segments/0/departure",
                json={"actor": "老赵", "idempotency_key": "dep-seg-00", "occurred_at": "2026-09-15T08:05:00Z"})
    client.post(f"/api/mortuary/transport-trips/{trip['id']}/segments/0/arrival",
                json={"actor": "老赵", "idempotency_key": "arr-seg-00", "occurred_at": "2026-09-15T09:10:00Z",
                      "observed_seal_code": "SEAL-A"})
    overview = client.get("/api/mortuary/transport-trips/coordination").json()
    entry = next(item for item in overview["trips"] if item["trip_id"] == trip["id"])
    assert entry["current_carrier"] == "安顺接运队"
    assert entry["pending_confirmations"][0]["confirmer"] == "station-master-qian"
    assert entry["pending_confirmations"][0]["station"] == "县级殡仪站"
    overdue_only = client.get("/api/mortuary/transport-trips/coordination?overdue_only=true").json()
    assert any(item["trip_id"] == trip["id"] for item in overdue_only["trips"])


def test_cancel_freezes_open_segments(client):
    case = create_case(client, "TRIP-CASE-003")
    trip = two_segment_trip(client, case["id"], ref="TRIP-003", key="trip-create-003")
    client.post(f"/api/mortuary/transport-trips/{trip['id']}/segments/0/departure",
                json={"actor": "老赵", "idempotency_key": "dep-seg-00", "occurred_at": "2026-09-15T08:05:00Z"})
    cancel = client.post(f"/api/mortuary/transport-trips/{trip['id']}/cancel",
                         json={"actor": "dispatcher-lin", "idempotency_key": "cancel-0", "reason": "家属改期"})
    assert cancel.status_code == 200
    detail = cancel.json()
    assert detail["status"] == "cancelled"
    assert {segment["status"] for segment in detail["segments"]} == {"cancelled"}
    # 取消是不可覆盖事件，取消后再报事件一律拒绝
    blocked = client.post(f"/api/mortuary/transport-trips/{trip['id']}/segments/0/delays",
                          json={"actor": "老赵", "idempotency_key": "delay-after-cancel",
                                "occurred_at": "2026-09-15T08:30:00Z", "delay_minutes": 10, "reason": "交通管制"})
    assert blocked.status_code == 409
    assert any(event["event_type"] == "trip.cancelled" for event in detail["events"])


def test_reroute_keeps_original_plan_and_recomputes_reservations(client):
    case = create_case(client, "TRIP-CASE-004")
    trip = two_segment_trip(client, case["id"], ref="TRIP-004", key="trip-create-004")

    assert client.post("/api/mortuary/resources?actor=scheduler",
                       json={"code": "COLD-1", "name": "市馆冷藏室1号", "kind": "cold_storage",
                             "site_code": "CITY", "capacity": 2, "attributes": {}}).status_code == 201
    reservation = client.post("/api/mortuary/reservations", json={
        "resource_code": "COLD-1", "case_id": case["id"],
        "start_at": "2026-09-15T10:00:00Z", "end_at": "2026-09-15T10:30:00Z",
        "purpose": "到馆入库", "created_by": "scheduler", "idempotency_key": "cold-resv-001"})
    assert reservation.status_code == 201, reservation.text

    # 第一段走完并确认
    client.post(f"/api/mortuary/transport-trips/{trip['id']}/segments/0/departure",
                json={"actor": "老赵", "idempotency_key": "dep-seg-00", "occurred_at": "2026-09-15T08:05:00Z"})
    client.post(f"/api/mortuary/transport-trips/{trip['id']}/segments/0/arrival",
                json={"actor": "老赵", "idempotency_key": "arr-seg-00", "occurred_at": "2026-09-15T09:05:00Z",
                      "observed_seal_code": "SEAL-A"})
    client.post(f"/api/mortuary/transport-trips/{trip['id']}/segments/0/confirm",
                json={"confirmed_by": "station-master-qian", "idempotency_key": "conf-seg-00",
                      "observed_seal_code": "SEAL-A"})

    # 改线：已完成段原样保留，未发车段替换为经停郊区的两段
    reroute = client.post(f"/api/mortuary/transport-trips/{trip['id']}/reroute", json={
        "actor": "dispatcher-lin", "idempotency_key": "reroute-001", "reason": "市区管制绕行",
        "segments": [
            {"from_station": "市第二医院太平间", "to_station": "县级殡仪站", "carrier": "安顺接运队",
             "vehicle_code": "VAN-01", "driver": "老赵", "planned_depart_at": "2026-09-15T08:00:00Z",
             "planned_arrive_at": "2026-09-15T09:00:00Z", "seal_code": "SEAL-A", "confirmer": "station-master-qian"},
            {"from_station": "县级殡仪站", "to_station": "郊区交接点", "carrier": "市级联运公司",
             "vehicle_code": "VAN-02", "driver": "老钱", "planned_depart_at": "2026-09-15T09:30:00Z",
             "planned_arrive_at": "2026-09-15T10:00:00Z", "seal_code": "SEAL-B", "confirmer": "station-master-qian"},
            {"from_station": "郊区交接点", "to_station": "市馆冷藏室", "carrier": "市级联运公司",
             "vehicle_code": "VAN-02", "driver": "老钱", "planned_depart_at": "2026-09-15T10:30:00Z",
             "planned_arrive_at": "2026-09-15T12:30:00Z", "seal_code": "SEAL-B", "confirmer": "keeper-wang"},
        ]})
    assert reroute.status_code == 200, reroute.text
    detail = reroute.json()
    assert detail["plan_version"] == 2
    assert len(detail["segments"]) == 3

    # 原计划完整保留为版本快照
    versions = detail["plan_versions"]
    assert [version["version"] for version in versions] == [1, 2]
    assert versions[0]["plan"][1]["to_station"] == "市馆冷藏室"
    assert versions[1]["reason"] == "市区管制绕行"

    # 旧的未发车分段被标记取代而不是删除
    raw_statuses = [tuple(row) for row in _open_connection(client).execute(
        "SELECT sequence_index,status,superseded,plan_version FROM transport_segments WHERE trip_id=? ORDER BY plan_version,sequence_index",
        (trip["id"],)).fetchall()]
    assert (1, "awaiting", 1, 1) in raw_statuses  # 原第二段仍可追溯

    # 受影响预约被重新计算：10:00 旧计划在县站途中，新计划已到郊区交接点
    impacts = detail["reservation_impacts"]
    assert len(impacts) == 1
    assert impacts[0]["reservation_id"] == reservation.json()["id"]
    assert impacts[0]["impact"] == "needs_reschedule"
    assert impacts[0]["new_eta_at"] == "2026-09-15T10:00:00+00:00"

    # 幂等：重复改线不再生成新版本或新影响
    client.post(f"/api/mortuary/transport-trips/{trip['id']}/reroute", json={
        "actor": "dispatcher-lin", "idempotency_key": "reroute-001", "reason": "市区管制绕行",
        "segments": [
            {"from_station": "市第二医院太平间", "to_station": "县级殡仪站", "carrier": "安顺接运队",
             "vehicle_code": "VAN-01", "driver": "老赵", "planned_depart_at": "2026-09-15T08:00:00Z",
             "planned_arrive_at": "2026-09-15T09:00:00Z", "seal_code": "SEAL-A", "confirmer": "station-master-qian"},
            {"from_station": "县级殡仪站", "to_station": "郊区交接点", "carrier": "市级联运公司",
             "vehicle_code": "VAN-02", "driver": "老钱", "planned_depart_at": "2026-09-15T09:30:00Z",
             "planned_arrive_at": "2026-09-15T10:00:00Z", "seal_code": "SEAL-B", "confirmer": "station-master-qian"},
            {"from_station": "郊区交接点", "to_station": "市馆冷藏室", "carrier": "市级联运公司",
             "vehicle_code": "VAN-02", "driver": "老钱", "planned_depart_at": "2026-09-15T10:30:00Z",
             "planned_arrive_at": "2026-09-15T12:30:00Z", "seal_code": "SEAL-B", "confirmer": "keeper-wang"},
        ]})
    detail = client.get(f"/api/mortuary/transport-trips/{trip['id']}").json()
    assert detail["plan_version"] == 2
    assert len(detail["reservation_impacts"]) == 1

    # 不允许改动已完成的第一段
    tampered = client.post(f"/api/mortuary/transport-trips/{trip['id']}/reroute", json={
        "actor": "dispatcher-lin", "idempotency_key": "reroute-bad", "reason": "篡改首段",
        "segments": [
            {"from_station": "市第二医院太平间", "to_station": "县级殡仪站", "carrier": "安顺接运队",
             "vehicle_code": "VAN-01", "driver": "老赵", "planned_depart_at": "2026-09-15T08:10:00Z",
             "planned_arrive_at": "2026-09-15T09:00:00Z", "seal_code": "SEAL-A", "confirmer": "station-master-qian"},
            {"from_station": "县级殡仪站", "to_station": "郊区交接点", "carrier": "市级联运公司",
             "vehicle_code": "VAN-02", "driver": "老钱", "planned_depart_at": "2026-09-15T09:30:00Z",
             "planned_arrive_at": "2026-09-15T10:00:00Z", "seal_code": "SEAL-B", "confirmer": "station-master-qian"},
            {"from_station": "郊区交接点", "to_station": "市馆冷藏室", "carrier": "市级联运公司",
             "vehicle_code": "VAN-02", "driver": "老钱", "planned_depart_at": "2026-09-15T10:30:00Z",
             "planned_arrive_at": "2026-09-15T12:30:00Z", "seal_code": "SEAL-B", "confirmer": "keeper-wang"},
        ]})
    assert tampered.status_code == 409

    # 新路线可以继续走完全程
    for sequence_index, confirmer, seal, arrive_at in (
        (1, "station-master-qian", "SEAL-B", "2026-09-15T10:00:00Z"),
        (2, "keeper-wang", "SEAL-B", "2026-09-15T12:30:00Z"),
    ):
        assert client.post(f"/api/mortuary/transport-trips/{trip['id']}/segments/{sequence_index}/departure",
                           json={"actor": "老钱", "idempotency_key": f"dep-seg-{sequence_index:02d}",
                                 "occurred_at": "2026-09-15T09:35:00Z" if sequence_index == 1 else "2026-09-15T10:35:00Z"}).status_code == 200
        assert client.post(f"/api/mortuary/transport-trips/{trip['id']}/segments/{sequence_index}/arrival",
                           json={"actor": "老钱", "idempotency_key": f"arr-seg-{sequence_index:02d}", "occurred_at": arrive_at,
                                 "observed_seal_code": seal}).status_code == 200
        assert client.post(f"/api/mortuary/transport-trips/{trip['id']}/segments/{sequence_index}/confirm",
                           json={"confirmed_by": confirmer, "idempotency_key": f"conf-seg-{sequence_index:02d}",
                                 "observed_seal_code": seal}).status_code == 200
    assert client.get(f"/api/mortuary/transport-trips/{trip['id']}").json()["status"] == "completed"


def test_trip_resumes_after_service_restart(client):
    from app.database import database_path

    case = create_case(client, "TRIP-CASE-005")
    trip = two_segment_trip(client, case["id"], ref="TRIP-005", key="trip-create-005")
    client.post(f"/api/mortuary/transport-trips/{trip['id']}/segments/0/departure",
                json={"actor": "老赵", "idempotency_key": "dep-seg-00", "occurred_at": "2026-09-15T08:05:00Z"})

    # 模拟服务重启：另开一个到同一数据库的连接，状态全部来自持久层
    restarted = sqlite3.connect(database_path())
    restarted.row_factory = sqlite3.Row
    row = restarted.execute("SELECT status,current_sequence FROM transport_trips WHERE id=?", (trip["id"],)).fetchone()
    assert row["status"] == "in_progress" and row["current_sequence"] == 0
    events = restarted.execute("SELECT event_type FROM transport_events WHERE trip_id=? ORDER BY id", (trip["id"],)).fetchall()
    assert [item[0] for item in events] == ["trip.created", "segment.departed"]
    restarted.close()

    # 重启后继续未完成行程
    assert client.post(f"/api/mortuary/transport-trips/{trip['id']}/segments/0/arrival",
                       json={"actor": "老赵", "idempotency_key": "arr-seg-00", "occurred_at": "2026-09-15T09:05:00Z",
                             "observed_seal_code": "SEAL-A"}).status_code == 200
    assert client.post(f"/api/mortuary/transport-trips/{trip['id']}/segments/0/confirm",
                       json={"confirmed_by": "station-master-qian", "idempotency_key": "conf-seg-00",
                             "observed_seal_code": "SEAL-A"}).status_code == 200
    assert client.post(f"/api/mortuary/transport-trips/{trip['id']}/segments/1/departure",
                       json={"actor": "老钱", "idempotency_key": "dep-seg-01", "occurred_at": "2026-09-15T09:35:00Z"}).status_code == 200
    assert client.post(f"/api/mortuary/transport-trips/{trip['id']}/segments/1/arrival",
                       json={"actor": "老钱", "idempotency_key": "arr-seg-01", "occurred_at": "2026-09-15T10:20:00Z",
                             "observed_seal_code": "SEAL-B"}).status_code == 200
    assert client.post(f"/api/mortuary/transport-trips/{trip['id']}/segments/1/confirm",
                       json={"confirmed_by": "keeper-wang", "idempotency_key": "conf-seg-01",
                             "observed_seal_code": "SEAL-B"}).status_code == 200
    assert client.get(f"/api/mortuary/transport-trips/{trip['id']}").json()["status"] == "completed"


def _open_connection(client) -> sqlite3.Connection:
    del client
    from app.database import database_path

    connection = sqlite3.connect(database_path())
    connection.row_factory = sqlite3.Row
    return connection
