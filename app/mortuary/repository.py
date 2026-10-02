from __future__ import annotations

import json
import sqlite3
from typing import Any


SCHEMA = r''' 
CREATE TABLE IF NOT EXISTS mortuary_cases (
 id INTEGER PRIMARY KEY AUTOINCREMENT, external_ref TEXT NOT NULL UNIQUE,
 decedent_name TEXT NOT NULL, identity_number TEXT, death_time TEXT NOT NULL,
 received_from TEXT NOT NULL, family_contact TEXT NOT NULL, family_phone TEXT NOT NULL,
 special_notes TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'registered',
 current_location TEXT NOT NULL DEFAULT '', version INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS custody_transfers (
 id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER NOT NULL REFERENCES mortuary_cases(id),
 from_location TEXT NOT NULL, to_location TEXT NOT NULL, seal_code TEXT NOT NULL,
 requested_by TEXT NOT NULL, accepted_by TEXT NOT NULL DEFAULT '', observed_seal_code TEXT NOT NULL DEFAULT '',
 condition_note TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
 idempotency_key TEXT NOT NULL, requested_at TEXT NOT NULL, accepted_at TEXT,
 UNIQUE(case_id,idempotency_key)
);
CREATE TABLE IF NOT EXISTS facility_resources (
 id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
 kind TEXT NOT NULL, site_code TEXT NOT NULL, capacity INTEGER NOT NULL,
 attributes_json TEXT NOT NULL DEFAULT '{}', active INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS facility_reservations (
 id INTEGER PRIMARY KEY AUTOINCREMENT, resource_id INTEGER NOT NULL REFERENCES facility_resources(id),
 case_id INTEGER NOT NULL REFERENCES mortuary_cases(id), start_at TEXT NOT NULL, end_at TEXT NOT NULL,
 purpose TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'confirmed', created_by TEXT NOT NULL,
 idempotency_key TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(resource_id,idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_reservation_window ON facility_reservations(resource_id,start_at,end_at,status);
CREATE TABLE IF NOT EXISTS funeral_service_orders (
 id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER NOT NULL REFERENCES mortuary_cases(id),
 service_code TEXT NOT NULL, quantity INTEGER NOT NULL, unit_price_cents INTEGER NOT NULL,
 amount_cents INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'draft', requested_by TEXT NOT NULL,
 notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS burial_rights (
 id INTEGER PRIMARY KEY AUTOINCREMENT, plot_code TEXT NOT NULL UNIQUE, holder_name TEXT NOT NULL,
 holder_identity TEXT NOT NULL, starts_on TEXT NOT NULL, expires_on TEXT NOT NULL,
 case_id INTEGER REFERENCES mortuary_cases(id), status TEXT NOT NULL DEFAULT 'active',
 version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS burial_right_renewals (
 id INTEGER PRIMARY KEY AUTOINCREMENT, right_id INTEGER NOT NULL REFERENCES burial_rights(id),
 previous_expires_on TEXT NOT NULL, new_expires_on TEXT NOT NULL, years INTEGER NOT NULL,
 payment_reference TEXT NOT NULL UNIQUE, handled_by TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS invoices (
 id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER NOT NULL REFERENCES mortuary_cases(id),
 amount_cents INTEGER NOT NULL, paid_cents INTEGER NOT NULL DEFAULT 0,
 status TEXT NOT NULL DEFAULT 'issued', created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS invoice_items (
 invoice_id INTEGER NOT NULL REFERENCES invoices(id), order_id INTEGER NOT NULL UNIQUE REFERENCES funeral_service_orders(id),
 amount_cents INTEGER NOT NULL, PRIMARY KEY(invoice_id,order_id)
);
CREATE TABLE IF NOT EXISTS payments (
 id INTEGER PRIMARY KEY AUTOINCREMENT, invoice_id INTEGER NOT NULL REFERENCES invoices(id),
 amount_cents INTEGER NOT NULL, channel TEXT NOT NULL, external_reference TEXT NOT NULL UNIQUE,
 received_by TEXT NOT NULL, received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mortuary_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, aggregate_type TEXT NOT NULL, aggregate_id TEXT NOT NULL,
 event_type TEXT NOT NULL, actor TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mortuary_event ON mortuary_events(aggregate_type,aggregate_id,id);
CREATE TABLE IF NOT EXISTS transport_trips (
 id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER NOT NULL REFERENCES mortuary_cases(id),
 origin_station TEXT NOT NULL, destination_station TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'planned', route_version INTEGER NOT NULL DEFAULT 1,
 planned_start_at TEXT NOT NULL, planned_end_at TEXT NOT NULL,
 created_by TEXT NOT NULL, idempotency_key TEXT NOT NULL,
 completed_at TEXT, cancelled_at TEXT, cancel_reason TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(case_id,idempotency_key)
);
CREATE TABLE IF NOT EXISTS transport_legs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, trip_id INTEGER NOT NULL REFERENCES transport_trips(id),
 route_version INTEGER NOT NULL, seq INTEGER NOT NULL,
 from_station TEXT NOT NULL, to_station TEXT NOT NULL,
 vehicle_code TEXT NOT NULL, carrier TEXT NOT NULL DEFAULT '',
 seal_code TEXT NOT NULL,
 planned_start_at TEXT NOT NULL, planned_end_at TEXT NOT NULL,
 confirm_roles_json TEXT NOT NULL DEFAULT '[]',
 status TEXT NOT NULL DEFAULT 'pending',
 departed_at TEXT, arrived_at TEXT,
 confirmed_by TEXT NOT NULL DEFAULT '', confirmer_role TEXT NOT NULL DEFAULT '',
 observed_seal_code TEXT NOT NULL DEFAULT '', condition_note TEXT NOT NULL DEFAULT '',
 confirmed_at TEXT, rejected_at TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(trip_id,route_version,seq)
);
CREATE INDEX IF NOT EXISTS idx_transport_leg_trip ON transport_legs(trip_id,route_version,seq);
CREATE TABLE IF NOT EXISTS transport_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, trip_id INTEGER NOT NULL REFERENCES transport_trips(id),
 leg_id INTEGER REFERENCES transport_legs(id),
 route_version INTEGER NOT NULL, seq INTEGER,
 event_type TEXT NOT NULL, actor TEXT NOT NULL,
 occurred_at TEXT NOT NULL, reported_at TEXT NOT NULL,
 idempotency_key TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL,
 UNIQUE(trip_id,event_type,idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_transport_event_trip ON transport_events(trip_id,occurred_at,id);
CREATE INDEX IF NOT EXISTS idx_transport_event_leg ON transport_events(leg_id,id);
CREATE TABLE IF NOT EXISTS transport_route_versions (
 id INTEGER PRIMARY KEY AUTOINCREMENT, trip_id INTEGER NOT NULL REFERENCES transport_trips(id),
 version INTEGER NOT NULL, reason TEXT NOT NULL DEFAULT '',
 legs_json TEXT NOT NULL, changed_by TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL,
 UNIQUE(trip_id,version)
);
CREATE TABLE IF NOT EXISTS transport_impacts (
 id INTEGER PRIMARY KEY AUTOINCREMENT, trip_id INTEGER NOT NULL REFERENCES transport_trips(id),
 route_version INTEGER NOT NULL, reservation_id INTEGER NOT NULL REFERENCES facility_reservations(id),
 resource_code TEXT NOT NULL, start_at TEXT NOT NULL, end_at TEXT NOT NULL,
 detail TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transport_impact_trip ON transport_impacts(trip_id,route_version);
'''


class MortuaryRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def ensure_schema(self) -> None:
        self.connection.executescript(SCHEMA)

    @staticmethod
    def one(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return None if row is None else dict(row)

    def event(self, kind: str, aggregate_id: int | str, event_type: str, actor: str, payload: dict[str, Any], now: str) -> None:
        self.connection.execute("INSERT INTO mortuary_events(aggregate_type,aggregate_id,event_type,actor,payload_json,created_at) VALUES(?,?,?,?,?,?)", (kind, str(aggregate_id), event_type, actor, json.dumps(payload, ensure_ascii=False, sort_keys=True), now))

    def case(self, case_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM mortuary_cases WHERE id=?", (case_id,)).fetchone())

    def case_ref(self, ref: str) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM mortuary_cases WHERE external_ref=?", (ref,)).fetchone())

    def transfer(self, transfer_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM custody_transfers WHERE id=?", (transfer_id,)).fetchone())

    def transfer_key(self, case_id: int, key: str) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM custody_transfers WHERE case_id=? AND idempotency_key=?", (case_id, key)).fetchone())

    def resource_code(self, code: str) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM facility_resources WHERE code=?", (code,)).fetchone())

    def resource(self, resource_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM facility_resources WHERE id=?", (resource_id,)).fetchone())

    def reservation_key(self, resource_id: int, key: str) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM facility_reservations WHERE resource_id=? AND idempotency_key=?", (resource_id, key)).fetchone())

    def reservation(self, reservation_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT r.*,f.code resource_code,f.kind resource_kind FROM facility_reservations r JOIN facility_resources f ON f.id=r.resource_id WHERE r.id=?", (reservation_id,)).fetchone())

    def conflicts(self, resource_id: int, start_at: str, end_at: str) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM facility_reservations WHERE resource_id=? AND status='confirmed' AND start_at>=? AND start_at<? ORDER BY start_at", (resource_id, start_at, end_at)).fetchall()
        return [dict(row) for row in rows]

    def order(self, order_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM funeral_service_orders WHERE id=?", (order_id,)).fetchone())

    def right(self, right_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM burial_rights WHERE id=?", (right_id,)).fetchone())

    def right_plot(self, plot_code: str) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM burial_rights WHERE plot_code=?", (plot_code,)).fetchone())

    def invoice(self, invoice_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone())

    def timeline(self, kind: str, aggregate_id: int | str) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM mortuary_events WHERE aggregate_type=? AND aggregate_id=? ORDER BY id", (kind, str(aggregate_id))).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def trip(self, trip_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM transport_trips WHERE id=?", (trip_id,)).fetchone())

    def trip_key(self, case_id: int, key: str) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM transport_trips WHERE case_id=? AND idempotency_key=?", (case_id, key)).fetchone())

    def active_trip_for_case(self, case_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM transport_trips WHERE case_id=? AND status IN ('planned','in_progress') ORDER BY id DESC LIMIT 1", (case_id,)).fetchone())

    def list_trips(self, status: str | None, limit: int) -> list[dict[str, Any]]:
        sql = "SELECT * FROM transport_trips"
        params: list[Any] = []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [dict(row) for row in self.connection.execute(sql, params).fetchall()]

    def legs(self, trip_id: int, route_version: int) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM transport_legs WHERE trip_id=? AND route_version=? ORDER BY seq", (trip_id, route_version)).fetchall()
        return [dict(row) for row in rows]

    def leg(self, leg_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM transport_legs WHERE id=?", (leg_id,)).fetchone())

    def transport_event_key(self, trip_id: int, event_type: str, key: str) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM transport_events WHERE trip_id=? AND event_type=? AND idempotency_key=?", (trip_id, event_type, key)).fetchone())

    def transport_events(self, trip_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM transport_events WHERE trip_id=? ORDER BY id", (trip_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def route_versions(self, trip_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM transport_route_versions WHERE trip_id=? ORDER BY version", (trip_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["legs"] = json.loads(item.pop("legs_json"))
            result.append(item)
        return result

    def impacts(self, trip_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM transport_impacts WHERE trip_id=? ORDER BY route_version,id", (trip_id,)).fetchall()
        return [dict(row) for row in rows]

    def trip_reservations(self, case_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT r.*,f.code resource_code,f.kind resource_kind FROM facility_reservations r JOIN facility_resources f ON f.id=r.resource_id "
            "WHERE r.case_id=? AND r.status='confirmed' ORDER BY r.start_at",
            (case_id,),
        ).fetchall()
        return [dict(row) for row in rows]
