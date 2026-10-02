from fastapi import APIRouter, Query

from app.core.errors import ValidationError
from app.mortuary.schemas import BurialRightCreate, BurialRightRenew, CaseCreate, CustodyAccept, CustodyTransferCreate, InvoiceCreate, PaymentCreate, ReservationCreate, ResourceCreate, ServiceOrderCreate, TransportCancel, TransportEventReport, TransportLegConfirm, TransportReroute, TransportTripCreate, TransportVehicleChange
from app.mortuary.service import MortuaryService
from app.mortuary.transport import LIVE_EVENT_TYPES, TransportService

router = APIRouter(prefix="/api/mortuary", tags=["mortuary"])

@router.post("/cases", status_code=201)
def create_case(payload: CaseCreate, actor: str = Query(min_length=2, max_length=80)) -> dict:
    return MortuaryService().create_case(payload.model_dump(), actor)

@router.get("/cases")
def list_cases(status: str | None = None, limit: int = Query(default=100, ge=1, le=500)) -> list[dict]:
    return MortuaryService().list_cases(status, limit)

@router.get("/cases/{case_id}")
def get_case(case_id: int) -> dict:
    return MortuaryService().get_case(case_id)

@router.post("/cases/{case_id}/custody-transfers", status_code=201)
def request_transfer(case_id: int, payload: CustodyTransferCreate) -> dict:
    return MortuaryService().request_transfer(case_id, payload.model_dump())

@router.post("/custody-transfers/{transfer_id}/accept")
def accept_transfer(transfer_id: int, payload: CustodyAccept) -> dict:
    return MortuaryService().accept_transfer(transfer_id, payload.model_dump())

@router.post("/resources", status_code=201)
def create_resource(payload: ResourceCreate, actor: str = Query(min_length=2, max_length=80)) -> dict:
    return MortuaryService().create_resource(payload.model_dump(mode="json"), actor)

@router.get("/resources")
def resources(kind: str | None = None) -> list[dict]:
    return MortuaryService().list_resources(kind)

@router.post("/reservations", status_code=201)
def reserve(payload: ReservationCreate) -> dict:
    return MortuaryService().reserve(payload.model_dump())

@router.post("/reservations/{reservation_id}/cancel")
def cancel(reservation_id: int, actor: str = Query(min_length=2), reason: str = Query(min_length=2, max_length=500)) -> dict:
    return MortuaryService().cancel_reservation(reservation_id, actor, reason)

@router.post("/service-orders", status_code=201)
def order(payload: ServiceOrderCreate) -> dict:
    return MortuaryService().add_order(payload.model_dump())

@router.post("/service-orders/{order_id}/confirm")
def confirm_order(order_id: int, actor: str = Query(min_length=2, max_length=80)) -> dict:
    return MortuaryService().confirm_order(order_id, actor)

@router.post("/burial-rights", status_code=201)
def create_right(payload: BurialRightCreate, actor: str = Query(min_length=2, max_length=80)) -> dict:
    return MortuaryService().create_right(payload.model_dump(), actor)

@router.post("/burial-rights/{right_id}/renew")
def renew_right(right_id: int, payload: BurialRightRenew) -> dict:
    return MortuaryService().renew_right(right_id, payload.model_dump())

@router.post("/invoices", status_code=201)
def create_invoice(payload: InvoiceCreate) -> dict:
    return MortuaryService().create_invoice(payload.model_dump())

@router.get("/invoices/{invoice_id}")
def get_invoice(invoice_id: int) -> dict:
    return MortuaryService().get_invoice(invoice_id)

@router.post("/invoices/{invoice_id}/payments")
def pay(invoice_id: int, payload: PaymentCreate) -> dict:
    return MortuaryService().pay(invoice_id, payload.model_dump())


# ---------------------------------------------------------------- 运输行程

@router.post("/transport-trips", status_code=201)
def create_transport_trip(payload: TransportTripCreate) -> dict:
    return TransportService().create_trip(payload.model_dump())

@router.get("/transport-trips")
def list_transport_trips(status: str | None = None, limit: int = Query(default=100, ge=1, le=500)) -> list[dict]:
    return TransportService().list_trips(status, limit)

@router.get("/transport-trips/overview")
def transport_overview() -> dict:
    return TransportService().overview()

@router.get("/transport-trips/{trip_id}")
def get_transport_trip(trip_id: int) -> dict:
    return TransportService().get_trip(trip_id)

@router.post("/transport-trips/{trip_id}/legs/{seq}/events/{event_type}")
def report_transport_event(trip_id: int, seq: int, event_type: str, payload: TransportEventReport) -> dict:
    if event_type not in LIVE_EVENT_TYPES:
        raise ValidationError(f"事件类型必须是 {sorted(LIVE_EVENT_TYPES)}")
    return TransportService().report_event(trip_id, seq, event_type, payload.model_dump())

@router.post("/transport-trips/{trip_id}/legs/{seq}/vehicle-change")
def change_transport_vehicle(trip_id: int, seq: int, payload: TransportVehicleChange) -> dict:
    return TransportService().change_vehicle(trip_id, seq, payload.model_dump())

@router.post("/transport-trips/{trip_id}/legs/{seq}/confirm")
def confirm_transport_leg(trip_id: int, seq: int, payload: TransportLegConfirm) -> dict:
    return TransportService().confirm_leg(trip_id, seq, payload.model_dump())

@router.post("/transport-trips/{trip_id}/reroute")
def reroute_transport_trip(trip_id: int, payload: TransportReroute) -> dict:
    return TransportService().reroute(trip_id, payload.model_dump())

@router.post("/transport-trips/{trip_id}/cancel")
def cancel_transport_trip(trip_id: int, payload: TransportCancel) -> dict:
    return TransportService().cancel_trip(trip_id, payload.model_dump())
