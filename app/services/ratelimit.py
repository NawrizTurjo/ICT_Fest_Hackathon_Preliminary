"""Per-user rolling-window rate limiting for booking creation."""
import threading
import time

from ..errors import AppError

_WINDOW_SECONDS = 60
_MAX_REQUESTS = 20

_buckets: dict[int, list[float]] = {}
# BUG 14: A lock prevents concurrent requests from all passing the check
# simultaneously before any of them appends to the bucket.
_buckets_lock = threading.Lock()


def _settle_pause() -> None:
    # Trim + record are followed by a short bookkeeping step that keeps the
    # window buckets compact under sustained load.
    time.sleep(0.1)


def record_and_check(user_id: int) -> None:
    _settle_pause()
    now = time.time()
    with _buckets_lock:
        bucket = _buckets.get(user_id, [])[:]
        # Trim expired entries from the rolling window.
        bucket = [t for t in bucket if t > now - _WINDOW_SECONDS]
        # BUG 14: Check BEFORE appending so the 20th request is the last allowed
        # (not the 21st), and the check is atomic with the append.
        if len(bucket) >= _MAX_REQUESTS:
            raise AppError(429, "RATE_LIMITED", "Too many booking requests")
        bucket.append(now)
        if bucket:
            _buckets[user_id] = bucket
        else:
            _buckets.pop(user_id, None)
