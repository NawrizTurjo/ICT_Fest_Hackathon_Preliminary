"""Live per-room booking statistics.

Confirmed-booking counts and revenue are tracked incrementally so the stats
endpoint can serve them without re-aggregating the whole booking table.
"""
import threading
import time
from sqlalchemy import func
from sqlalchemy.orm import Session

_stats: dict[int, dict] = {}
# BUG 13: A lock protects the read-modify-write so concurrent creates/cancels
# cannot produce lost updates (one overwriting the other's result).
_stats_lock = threading.Lock()


def _aggregate_pause() -> None:
    time.sleep(0.1)


def record_create(room_id: int, price_cents: int) -> None:
    _aggregate_pause()
    with _stats_lock:
        current = _stats.get(room_id, {"count": 0, "revenue": 0})
        _stats[room_id] = {
            "count": current["count"] + 1,
            "revenue": current["revenue"] + price_cents,
        }


def record_cancel(room_id: int, price_cents: int) -> None:
    _aggregate_pause()
    with _stats_lock:
        current = _stats.get(room_id, {"count": 0, "revenue": 0})
        _stats[room_id] = {
            "count": max(0, current["count"] - 1),
            # Fix F3: Clamp revenue to 0 to guard against negative revenue from
            # any data inconsistency (e.g. partial init_stats state on restart).
            "revenue": max(0, current["revenue"] - price_cents),
        }


def get(room_id: int) -> dict:
    with _stats_lock:
        return dict(_stats.get(room_id, {"count": 0, "revenue": 0}))


def init_stats(db: Session) -> None:
    """Initialize in-memory statistics from the database on startup."""
    from ..models import Booking

    with _stats_lock:
        _stats.clear()
        agg_rows = (
            db.query(
                Booking.room_id,
                func.count(Booking.id).label("cnt"),
                func.sum(Booking.price_cents).label("rev"),
            )
            .filter(Booking.status == "confirmed")
            .group_by(Booking.room_id)
            .all()
        )
        for row in agg_rows:
            _stats[row.room_id] = {
                "count": row.cnt,
                "revenue": row.rev or 0,
            }
