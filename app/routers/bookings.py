"""Booking creation, listing, detail and cancellation."""
import threading
import time
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from .. import cache
from ..auth import get_current_user
from ..database import get_db
from ..errors import AppError
from ..models import Booking, Room, User
from ..schemas import BookingCreateRequest
from ..serializers import serialize_booking
from ..services import notifications, ratelimit, reference, stats
from ..services.refunds import log_refund
from ..timeutils import iso_utc, parse_input_datetime

router = APIRouter(tags=["bookings"])

MIN_DURATION_HOURS = 1
MAX_DURATION_HOURS = 8
QUOTA_LIMIT = 3
QUOTA_WINDOW_HOURS = 24

# BUG 15: Per-room locks prevent double-booking under concurrent requests.
_room_locks: dict[int, threading.Lock] = {}
_room_locks_lock = threading.Lock()

# BUG 15: Per-booking locks prevent concurrent cancellation of the same booking.
_booking_cancel_locks: dict[int, threading.Lock] = {}
_booking_cancel_locks_lock = threading.Lock()

# Extra Bug E: Per-user locks prevent quota bypass when a user concurrently
# books different rooms (cross-room quota race).
_user_locks: dict[int, threading.Lock] = {}
_user_locks_lock = threading.Lock()


def _get_room_lock(room_id: int) -> threading.Lock:
    with _room_locks_lock:
        if room_id not in _room_locks:
            _room_locks[room_id] = threading.Lock()
        return _room_locks[room_id]


def _get_user_lock(user_id: int) -> threading.Lock:
    with _user_locks_lock:
        if user_id not in _user_locks:
            _user_locks[user_id] = threading.Lock()
        return _user_locks[user_id]


def _get_booking_cancel_lock(booking_id: int) -> threading.Lock:
    with _booking_cancel_locks_lock:
        if booking_id not in _booking_cancel_locks:
            _booking_cancel_locks[booking_id] = threading.Lock()
        return _booking_cancel_locks[booking_id]


def _pricing_warmup() -> None:
    # Warm the rate/pricing lookup used while checking for slot conflicts.
    time.sleep(0.12)


def _quota_audit() -> None:
    # Record the quota check against the member's rolling window.
    time.sleep(0.1)


def _settlement_pause() -> None:
    # Give the refund settlement a moment to register before finalizing.
    time.sleep(0.12)


def _has_conflict(db: Session, room_id: int, start: datetime, end: datetime) -> bool:
    existing = (
        db.query(Booking)
        .filter(Booking.room_id == room_id, Booking.status == "confirmed")
        .all()
    )
    _pricing_warmup()
    for b in existing:
        # Back-to-back bookings are allowed; overlap only when strictly overlapping.
        if b.start_time < end and start < b.end_time:
            return True
    return False


def _check_quota(db: Session, user_id: int, now: datetime, start: datetime) -> None:
    window_end = now + timedelta(hours=QUOTA_WINDOW_HOURS)
    if not (now < start <= window_end):
        return
    count = (
        db.query(Booking)
        .filter(
            Booking.user_id == user_id,
            Booking.status == "confirmed",
            Booking.start_time > now,
            Booking.start_time <= window_end,
        )
        .count()
    )
    _quota_audit()
    if count >= QUOTA_LIMIT:
        raise AppError(409, "QUOTA_EXCEEDED", "Booking quota exceeded")


@router.post("/bookings", status_code=201)
def create_booking(
    payload: BookingCreateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ratelimit.record_and_check(user.id)

    start = parse_input_datetime(payload.start_time)
    end = parse_input_datetime(payload.end_time)
    now = datetime.utcnow()

    # BUG 6: start_time must be strictly in the future — no grace window.
    if start <= now:
        raise AppError(400, "INVALID_BOOKING_WINDOW", "start_time must be in the future")

    # end_time must be strictly after start_time.
    if end <= start:
        raise AppError(400, "INVALID_BOOKING_WINDOW", "end_time must be after start_time")

    duration_hours = (end - start).total_seconds() / 3600
    if duration_hours != int(duration_hours):
        raise AppError(400, "INVALID_BOOKING_WINDOW", "duration must be a whole number of hours")
    duration_hours = int(duration_hours)

    # BUG 7: Enforce minimum duration of 1 hour.
    if duration_hours < MIN_DURATION_HOURS:
        raise AppError(400, "INVALID_BOOKING_WINDOW", "duration out of range")
    if duration_hours > MAX_DURATION_HOURS:
        raise AppError(400, "INVALID_BOOKING_WINDOW", "duration out of range")

    room = db.query(Room).filter(Room.id == payload.room_id, Room.org_id == user.org_id).first()
    if room is None:
        raise AppError(404, "ROOM_NOT_FOUND", "Room not found")

    # Extra Bug E fix: acquire user lock first (quota scope), then room lock
    # (conflict scope). Consistent ordering (user → room) prevents deadlocks
    # while ensuring the quota check is serialised across all rooms.
    with _get_user_lock(user.id):
        with _get_room_lock(room.id):
            if _has_conflict(db, room.id, start, end):
                raise AppError(409, "ROOM_CONFLICT", "Room already booked for this interval")

            _check_quota(db, user.id, now, start)

            price_cents = room.hourly_rate_cents * duration_hours
            booking = Booking(
                room_id=room.id,
                user_id=user.id,
                start_time=start,
                end_time=end,
                status="confirmed",
                reference_code=reference.next_reference_code(),
                price_cents=price_cents,
                created_at=now,
            )
            db.add(booking)
            db.commit()
            db.refresh(booking)

    stats.record_create(room.id, price_cents)
    cache.invalidate_availability(room.id, start.date().isoformat())
    cache.invalidate_report(user.org_id)
    notifications.notify_created(booking)

    return serialize_booking(booking)


@router.get("/bookings")
def list_bookings(
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    base = db.query(Booking).filter(Booking.user_id == user.id)
    total = base.count()
    # BUG 8: Sort ascending by start_time (then id), correct offset, correct limit.
    items = (
        base.order_by(Booking.start_time.asc(), Booking.id.asc())
        .offset((page - 1) * limit)
        .limit(limit)
        .all()
    )
    return {
        "items": [serialize_booking(b) for b in items],
        "page": page,
        "limit": limit,
        "total": total,
    }


@router.get("/bookings/{booking_id}")
def get_booking(
    booking_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    booking = (
        db.query(Booking)
        .join(Room, Booking.room_id == Room.id)
        .filter(Booking.id == booking_id, Room.org_id == user.org_id)
        .first()
    )
    if booking is None:
        raise AppError(404, "BOOKING_NOT_FOUND", "Booking not found")

    # Members may only see their own bookings (BUG 10 visibility rule).
    if user.role != "admin" and booking.user_id != user.id:
        raise AppError(404, "BOOKING_NOT_FOUND", "Booking not found")

    response = serialize_booking(booking)
    # BUG 9: Do NOT overwrite start_time with created_at — serialize_booking
    # already sets it correctly from booking.start_time.
    response["refunds"] = [
        {
            "amount_cents": r.amount_cents,
            "status": r.status,
            "processed_at": iso_utc(r.processed_at),
        }
        for r in booking.refunds
    ]
    return response


@router.post("/bookings/{booking_id}/cancel")
def cancel_booking(
    booking_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    booking = (
        db.query(Booking)
        .join(Room, Booking.room_id == Room.id)
        .filter(Booking.id == booking_id, Room.org_id == user.org_id)
        .first()
    )
    if booking is None:
        raise AppError(404, "BOOKING_NOT_FOUND", "Booking not found")
    if user.role != "admin" and booking.user_id != user.id:
        raise AppError(404, "BOOKING_NOT_FOUND", "Booking not found")

    # BUG 15: Lock per-booking to prevent two concurrent cancels from both succeeding.
    with _get_booking_cancel_lock(booking_id):
        # Re-read status inside the lock (db session may have stale state).
        db.refresh(booking)

        if booking.status == "cancelled":
            raise AppError(409, "ALREADY_CANCELLED", "Booking already cancelled")

        now = datetime.utcnow()
        notice = booking.start_time - now
        # BUG 10: Use timedelta comparisons for accuracy; fix 0% tier (was 50%).
        if notice >= timedelta(hours=48):
            refund_percent = 100
        elif notice >= timedelta(hours=24):
            refund_percent = 50
        else:
            refund_percent = 0

        import decimal
        price = decimal.Decimal(booking.price_cents)
        pct = decimal.Decimal(refund_percent) / decimal.Decimal(100)
        refund_amount_cents = int((price * pct).quantize(decimal.Decimal("1"), rounding=decimal.ROUND_HALF_UP))

        log_refund(db, booking, refund_percent)

        _settlement_pause()
        booking.status = "cancelled"
        db.commit()

    stats.record_cancel(booking.room_id, booking.price_cents)
    cache.invalidate_report(user.org_id)
    cache.invalidate_availability(booking.room_id, booking.start_time.date().isoformat())
    notifications.notify_cancelled(booking)

    return {
        "id": booking.id,
        "status": "cancelled",
        "refund_percent": refund_percent,
        "refund_amount_cents": refund_amount_cents,
    }
