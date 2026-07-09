"""Administrative reporting and export endpoints."""
from collections import defaultdict
from datetime import datetime, time, timedelta

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import cache
from ..auth import require_admin
from ..database import get_db
from ..errors import AppError
from ..models import Booking, Room, User
from ..services.export import generate_export

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/usage-report")
def usage_report(
    frm: str = Query(..., alias="from"),
    to: str = Query(...),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    cached = cache.get_report(admin.org_id, frm, to)
    if cached is not None:
        return cached

    try:
        from_date = datetime.strptime(frm, "%Y-%m-%d").date()
        to_date = datetime.strptime(to, "%Y-%m-%d").date()
    except ValueError:
        raise AppError(400, "INVALID_BOOKING_WINDOW", "Invalid date range")

    range_start = datetime.combine(from_date, time.min)
    range_end = datetime.combine(to_date + timedelta(days=1), time.min)

    rooms = db.query(Room).filter(Room.org_id == admin.org_id).order_by(Room.id.asc()).all()

    # Single aggregated query instead of N+1 per-room queries.
    agg_rows = (
        db.query(
            Booking.room_id,
            func.count(Booking.id).label("cnt"),
            func.sum(Booking.price_cents).label("rev"),
        )
        .join(Room, Booking.room_id == Room.id)
        .filter(
            Room.org_id == admin.org_id,
            Booking.status == "confirmed",
            Booking.start_time >= range_start,
            Booking.start_time < range_end,
        )
        .group_by(Booking.room_id)
        .all()
    )
    agg = {row.room_id: (row.cnt, row.rev or 0) for row in agg_rows}

    room_rows = []
    for room in rooms:
        cnt, rev = agg.get(room.id, (0, 0))
        room_rows.append(
            {
                "room_id": room.id,
                "room_name": room.name,
                "confirmed_bookings": cnt,
                "revenue_cents": rev,
            }
        )

    result = {"from": frm, "to": to, "rooms": room_rows}
    cache.set_report(admin.org_id, frm, to, result)
    return result


@router.get("/export")
def export(
    room_id: int | None = Query(None),
    include_all: bool = Query(False),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    if room_id is not None:
        room = db.query(Room).filter(Room.id == room_id, Room.org_id == admin.org_id).first()
        if room is None:
            raise AppError(404, "ROOM_NOT_FOUND", "Room not found")
    csv_body = generate_export(db, admin.org_id, admin.id, room_id, include_all)
    return Response(content=csv_body, media_type="text/csv")
