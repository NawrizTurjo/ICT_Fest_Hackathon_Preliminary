"""Refund bookkeeping.

When a booking is cancelled a refund is calculated from its price and the
applicable notice tier, then written to the refund ledger with a processed
status. Amounts are stored in whole cents.
"""
from datetime import datetime

from sqlalchemy.orm import Session

from ..models import Booking, RefundLog


def log_refund(db: Session, booking: Booking, percent: int) -> RefundLog:
    import decimal
    # Business rule: rounds to nearest cent, half-cents round up.
    price = decimal.Decimal(booking.price_cents)
    pct = decimal.Decimal(percent) / decimal.Decimal(100)
    amount_cents = int((price * pct).quantize(decimal.Decimal("1"), rounding=decimal.ROUND_HALF_UP))
    entry = RefundLog(
        booking_id=booking.id,
        amount_cents=amount_cents,
        status="processed",
        processed_at=datetime.utcnow(),
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry
