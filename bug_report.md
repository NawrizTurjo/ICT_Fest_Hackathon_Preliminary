# Bug Report — CoWork API

## Bug 1 — Easy | `app/auth.py` line 50 — Access token lifetime 60× too long

**File/Line:** `app/auth.py:50`

**Bug:** `timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES * 60)` — `ACCESS_TOKEN_EXPIRE_MINUTES` is 15, so this creates a timedelta of 900 minutes (15 hours) instead of 15 minutes (900 seconds).

**Impact:** Access tokens effectively never expire during normal use. The spec requires exactly 900 seconds.

**Fix:** `timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)` — removed the `* 60` multiplier.

---

## Bug 2 — Easy | `app/auth.py` line 97 — Revocation checks `sub` instead of `jti`

**File/Line:** `app/auth.py:86,97`

**Bug:** `revoke_access_token` correctly adds `payload["jti"]` to `_revoked_tokens`, but `get_token_payload` checks `payload.get("sub") in _revoked_tokens`. Since `sub` is the user id (never added to the set), no token is ever actually blocked. Worse, after logout any token with the same `sub` would be rejected if the check were swapped — blocking the entire user.

**Impact:** Logout has no effect; the presented token remains valid indefinitely.

**Fix:** Changed line 97 to `if payload.get("jti") in _revoked_tokens:`.

---

## Bug 3 — Easy | `app/routers/auth.py` lines 37-43 — Duplicate username returns 200 instead of 409

**File/Line:** `app/routers/auth.py:37-43`

**Bug:** When a user with the same username already exists in the org, the handler returns that user's data with HTTP 200 instead of raising an error. The spec mandates `409 USERNAME_TAKEN`.

**Impact:** Re-registration with an existing username silently succeeds instead of being rejected.

**Fix:** Replaced the `return {...}` block with `raise AppError(409, "USERNAME_TAKEN", "Username already taken")`.

---

## Bug 4 — Easy | `app/routers/auth.py` lines 81-93 — Refresh tokens are never invalidated

**File/Line:** `app/routers/auth.py:81-93`, `app/auth.py`

**Bug:** The `/auth/refresh` endpoint decodes the token and issues new tokens, but never records that the presented refresh token was used. The same refresh token can be presented repeatedly to obtain new access tokens indefinitely.

**Impact:** Refresh tokens are not single-use; the spec requires reuse → 401.

**Fix:** Added `_revoked_refresh_jtis: set[str]` in `auth.py` with `is_refresh_token_revoked()` and `revoke_refresh_token()` helpers. The refresh handler now checks the jti against this set and adds it before issuing new tokens.

---

## Bug 5 — Easy | `app/timeutils.py` lines 12-13 — Timezone offset stripped without converting

**File/Line:** `app/timeutils.py:12-13`

**Bug:** `dt.replace(tzinfo=None)` strips the timezone info but leaves the clock value unchanged. A datetime like `2026-01-01T15:00:00+06:00` (which is 09:00 UTC) would be stored as `2026-01-01T15:00:00` — 6 hours ahead of UTC.

**Impact:** Any input with a non-UTC offset is stored with the wrong time, breaking all time-based comparisons (conflict detection, quota windows, refund notice calculation).

**Fix:** `dt.astimezone(timezone.utc).replace(tzinfo=None)` — converts to UTC first, then strips tzinfo.

---

## Bug 6 — Easy | `app/routers/bookings.py` line 86 — 5-minute grace window

**File/Line:** `app/routers/bookings.py:86`

**Bug:** `if start <= now - timedelta(seconds=300)` allows bookings with a `start_time` up to 5 minutes in the past. The spec states start_time must be strictly in the future with no grace window.

**Impact:** Bookings with past start times are accepted.

**Fix:** `if start <= now:` — strict future check with no tolerance.

---

## Bug 7 — Easy | `app/routers/bookings.py` line 93 — Missing minimum 1-hour duration check

**File/Line:** `app/routers/bookings.py:93`

**Bug:** Only the maximum (8 hours) is validated. The minimum of 1 hour is never checked, so a booking with `duration_hours = 0` (start == end would be caught, but e.g. 30 min) passes through.

**Impact:** Bookings shorter than 1 hour are accepted. With the `end <= start` guard also missing, zero-duration bookings were possible too.

**Fix:** Added `if end <= start: raise ...` and `if duration_hours < MIN_DURATION_HOURS: raise ...`.

---

## Bug 8 — Medium | `app/routers/bookings.py` lines 137-139 — Wrong sort, offset, and limit in list_bookings

**File/Line:** `app/routers/bookings.py:137-139`

**Bug:** Three compounding mistakes:
1. `.order_by(Booking.start_time.desc(), ...)` — spec requires ascending.
2. `.offset(page * limit)` — for page=1, this skips the first `limit` items entirely. Should be `(page - 1) * limit`.
3. `.limit(10)` — hardcoded instead of using the `limit` query parameter.

**Impact:** Page 1 always returns an empty result (offset skips everything). Sort order is reversed. Custom limit values are ignored.

**Fix:** `.order_by(Booking.start_time.asc(), Booking.id.asc()).offset((page - 1) * limit).limit(limit)`.

---

## Bug 9 — Medium | `app/routers/bookings.py` line 166 — `start_time` overwritten with `created_at`

**File/Line:** `app/routers/bookings.py:166`

**Bug:** `response["start_time"] = iso_utc(booking.created_at)` overwrites the correctly serialized `start_time` field (set by `serialize_booking`) with `created_at`. These are different fields.

**Impact:** `GET /bookings/{id}` returns `created_at` in the `start_time` field, making the response incorrect.

**Fix:** Removed that line entirely. `serialize_booking` already sets `start_time` from `booking.start_time`.

---

## Bug 10 — Medium | `app/routers/bookings.py` lines 201-206 — Wrong refund for < 24h notice

**File/Line:** `app/routers/bookings.py:201-206`

**Bug:** Two issues:
1. The `else` branch (notice < 24 hours) sets `refund_percent = 50` instead of `0`.
2. The ≥ 48h check uses `notice_hours > 48` where `notice_hours = int(notice.total_seconds() // 3600)`. This truncates and misses the exact 48-hour boundary (e.g. 48h 30min → notice_hours = 48 → falls into the 50% tier instead of 100%).

**Impact:** Customers who cancel with less than 24 hours notice receive a 50% refund they are not owed. Cancellations at exactly 48h may get the wrong tier.

**Fix:** Changed `else: refund_percent = 50` → `else: refund_percent = 0`. Replaced int-truncated hours check with `if notice >= timedelta(hours=48)`.

---

## Bug 11 — Hard | `app/services/notifications.py` lines 24-35 — AB/BA deadlock

**File/Line:** `app/services/notifications.py:24-35`

**Bug:** Lock acquisition order is inconsistent between the two functions:
- `notify_created`: acquires `_email_lock` → then `_audit_lock`
- `notify_cancelled`: acquires `_audit_lock` → then `_email_lock`

When a create and cancel happen concurrently (common during normal use), Thread A holds `_email_lock` waiting for `_audit_lock`, while Thread B holds `_audit_lock` waiting for `_email_lock`. Both threads block forever — the service hangs.

**Impact:** The service deadlocks under concurrent create + cancel, violating the liveness requirement.

**Fix:** Both functions now acquire `_email_lock` first, then `_audit_lock`, eliminating the cycle.

---

## Bug 12 — Hard | `app/services/reference.py` lines 17-21 — Reference code TOCTOU race

**File/Line:** `app/services/reference.py:17-21`

**Bug:**
```python
current = _counter["value"]   # Thread A reads 1000
_format_pause()                # Thread A sleeps 120ms
# Thread B also reads 1000, sleeps, writes 1001, returns "CW-001000"
_counter["value"] = current + 1  # Thread A writes 1001, returns "CW-001000"
```
Two concurrent booking requests read the same counter value and produce the same reference code.

**Impact:** Reference codes are not unique under concurrent creation, violating a hard requirement.

**Fix:** The read+increment is now inside a `threading.Lock()`. The sleep is moved outside the lock so it doesn't block other threads.

---

## Bug 13 — Hard | `app/services/stats.py` lines 15-26 — Stats TOCTOU lost-update race

**File/Line:** `app/services/stats.py:15-26`

**Bug:**
```python
current = _stats.get(room_id, ...)   # Thread A reads {count:5, revenue:5000}
_aggregate_pause()                    # Thread A sleeps 100ms
# Thread B also reads {count:5, revenue:5000}, writes {count:6, revenue:6000}
_stats[room_id] = {count: 6, ...}    # Thread A writes {count:6, ...}, clobbering B's update
```
Both threads read the same baseline; one update is lost.

**Impact:** Room stats become inconsistent with actual bookings after bursts of concurrent activity.

**Fix:** The `_aggregate_pause` is moved before the lock. Read-modify-write is now done atomically under `_stats_lock = threading.Lock()`.

---

## Bug 14 — Hard | `app/services/ratelimit.py` lines 18-26 — Rate limiter TOCTOU + off-by-one

**File/Line:** `app/services/ratelimit.py:18-26`

**Bug:** Two issues:
1. **Race:** `_settle_pause()` sleeps between trimming and appending. Concurrent requests can all pass the `len(bucket) > _MAX_REQUESTS` check before any of them has appended.
2. **Off-by-one:** The check runs *after* appending: `len(bucket) > 20` means the 22nd request (bucket length 21) triggers 429. The 21st request passes with a bucket of length 21 > 20... actually this means the 21st is blocked. But request 20 is appended (bucket = 20), check is 20 > 20 = False, passes. Request 21 is appended (bucket = 21), check 21 > 20 = True, blocked. So actually it's correct in serial... but the race still allows concurrent requests to all see a pre-append bucket and all pass.

**Impact:** Under concurrent load, more than 20 requests per window can succeed.

**Fix:** The entire trim+check+append is now inside `_buckets_lock`. The check is moved before append: `if len(bucket) >= _MAX_REQUESTS: raise`. Sleep moved outside the lock.

---

## Bug 15 — Hard | `app/routers/bookings.py` — No locking for concurrent booking/cancellation

**File/Line:** `app/routers/bookings.py:42-124` (create), `178-225` (cancel)

**Bug:** `_has_conflict` reads all bookings for a room, sleeps (`_pricing_warmup`), then the booking is inserted. Two concurrent requests for the same room both pass the conflict check before either commits, resulting in a double-booking. Similarly, `_check_quota` reads and sleeps before the booking is written.

For cancellation: two concurrent cancel requests both read `booking.status == "confirmed"`, both proceed to write `"cancelled"`, and both create a RefundLog — double refund.

**Impact:** Double-bookings and double-refunds are possible under concurrent load.

**Fix:** Added per-room `threading.Lock` (`_room_locks`) wrapping the conflict check + insert atomically. Added per-booking `threading.Lock` (`_booking_cancel_locks`) wrapping the cancel check + status update. `db.refresh(booking)` inside the cancel lock ensures the latest DB state is read after acquiring the lock.

---

## Bonus Fix — `app/services/refunds.py` line 17 — Truncation instead of round-half-up

**File/Line:** `app/services/refunds.py:17`

**Bug:** `int(refund_dollars * 100)` truncates toward zero instead of rounding to the nearest cent with half-cents rounding up (as specified by the business rules).

**Impact:** Refund amounts stored in the ledger may be 1 cent lower than the correct rounded value, and the `cancel` response (`refund_amount_cents`) could disagree with what's stored in the RefundLog.

**Fix:** Replaced float arithmetic with `decimal.Decimal` and `ROUND_HALF_UP` rounding.

---

## Extra Bug A — Medium | Report cache not invalidated on room/booking creation

**File/Line:** `app/routers/bookings.py:121` (booking creation), `app/routers/rooms.py:57` (room creation)

**Bug:** The application caches usage reports at `/admin/usage-report` by organization ID and date range. However, this cache was only invalidated when a booking was *cancelled* (via `cache.invalidate_report(user.org_id)`). It was never invalidated when a new room was created or when a new booking was confirmed.

**Impact:** Admins calling the usage report endpoint after a room or booking is created would receive stale cached data that does not reflect the current live state immediately, violating Rule 12.

**Fix:** Added `cache.invalidate_report(user.org_id)` to `create_booking` and `cache.invalidate_report(admin.org_id)` to `create_room`.

---

## Extra Bug B — Hard | Multi-Tenancy leakage in `/admin/export`

**File/Line:** `app/routers/admin.py:73`

**Bug:** The `/admin/export` endpoint allows admins to export bookings as a CSV. If a `room_id` query parameter is provided, and `include_all` is True, it fetches bookings via `fetch_bookings_raw(db, room_id)` which does not perform any multi-tenancy validation or organization ID checks. If `include_all` is False, it returns an empty CSV instead of raising a 404.

**Impact:** An admin of Org A could specify a `room_id` belonging to Org B, and retrieve a complete history of Org B's bookings, bypassing multi-tenancy controls and violating Rule 9 ("Cross-org resource IDs behave as non-existent (→ 404)").

**Fix:** Added validation in the `/admin/export` router endpoint: if `room_id` is provided, it verifies the room exists and belongs to the admin's organization, raising `404 ROOM_NOT_FOUND` if not.

---

## Extra Bug C — Hard | Live statistics state loss upon server restart

**File/Line:** `app/services/stats.py`, `app/main.py`

**Bug:** Room statistics are tracked incrementally in an in-memory dictionary `_stats`. However, if the server restarts, this dictionary was reset to empty. If bookings already existed in the SQLite database, stats would report 0 confirmed bookings and 0 revenue.

**Impact:** Live room statistics endpoint `/rooms/{id}/stats` becomes inconsistent with actual database bookings after server restarts, violating Rule 14.

**Fix:** Added an `init_stats(db)` startup hook in `app/main.py` that queries the SQLite database on initialization, computes the baseline for all confirmed bookings, and populates `_stats`.

---

## Extra Bug D — Medium | Banker's rounding in `/bookings/{id}/cancel` vs Round-Half-Up in `log_refund`

**File/Line:** `app/routers/bookings.py:246`

**Bug:** The refund calculation in `cancel_booking` used Python's built-in `round()`, which performs banker's rounding (rounding half to even). However, `log_refund` used `decimal.ROUND_HALF_UP` (standard rounding half up). For a booking price that yields a half-cent (e.g. 502.5 cents), `cancel_booking` rounded to `502` while `log_refund` wrote `503` to the DB.

**Impact:** A discrepancy where the response body's `refund_amount_cents` is different from the database's `RefundLog.amount_cents` (violating Rule 6: "the amount returned by the cancel response must equal the amount stored in the RefundLog").

**Fix:** Changed `cancel_booking`'s rounding logic to use `decimal.Decimal` with `ROUND_HALF_UP` to match `log_refund`.

---

## Extra Bug E — Hard | Cross-Room Quota Race Condition

**File/Line:** `app/routers/bookings.py:133-154`

**Bug:** The per-room lock (`_get_room_lock(room.id)`) correctly serialises concurrent booking requests **for the same room**, preventing double-bookings. However, the 3-booking quota (Rule 4) applies *across all rooms* in an org, not just one room. Because the locks are partitioned by `room_id`, concurrent requests to **different rooms** acquire different locks and execute simultaneously:

1. Thread 1 acquires `_room_locks[A]`, Thread 2 acquires `_room_locks[B]`, etc.
2. All threads call `_check_quota()`, which queries the DB and then sleeps for 100 ms (`_quota_audit()`).
3. Since they sleep concurrently and none has committed yet, all see `count = 0` and all pass the quota check.
4. All four threads commit — the user ends up with 4 confirmed bookings in the 24-hour window.

**Impact:** A user can exceed the 3-booking-per-24h quota by submitting simultaneous requests to different rooms, violating Rule 4. This is a hard concurrency bug exploitable with a simple parallel script.

**Fix:** Added a per-user lock (`_user_locks`) that is acquired **before** the per-room lock. The lock hierarchy is always `user → room` (consistent ordering prevents deadlocks). The quota check and booking commit now run under both locks, serialising all booking attempts for the same user regardless of which room they target.

```python
with _get_user_lock(user.id):          # serialises all of this user's bookings
    with _get_room_lock(room.id):       # prevents double-booking same room
        _check_quota(...)               # now safe: no concurrent sibling can pass
        db.add(booking)
        db.commit()
```

## Advanced Optimizations & Edge Case Fixes

---

### Fix 1: O(N) Memory Load in Conflict Check

**File:** `app/routers/bookings.py` (Function: `_has_conflict`)

**Bug:** The original implementation fetched all confirmed bookings for a room using `.all()` and then iterated over them in Python to check for overlapping time intervals. This caused an O(N) memory load proportional to the total number of bookings for a room, creating a risk of high memory consumption and slow conflict detection as booking volume grows.

**Fix:** Optimized `_has_conflict` to use a single O(1) DB-side interval query instead of loading all confirmed bookings into Python memory. The overlap condition (`existing.start_time < end AND start < existing.end_time`) is now evaluated entirely by the database engine, and the result is returned using `.first() is not None` — a boolean with no ORM object materialization.

```python
return (
    db.query(Booking)
    .filter(
        Booking.room_id == room_id,
        Booking.status == "confirmed",
        Booking.start_time < end,
        start < Booking.end_time,
    )
    .first()
    is not None
)
```

---

### Fix 2: N+1 Query Problem in Usage Report

**File:** `app/routers/admin.py` (Function: `usage_report`)

**Bug:** The original implementation first fetched all rooms for an org, then executed a separate `db.query(Booking)` inside a loop for each individual room. This is a classic N+1 query problem: for an org with N rooms, the endpoint fired N+1 database round-trips, causing severe performance degradation at scale.

**Fix:** Refactored `usage_report` to use a single `GROUP BY room_id` query with `func.count` and `func.sum` to fetch all aggregated data in one database call. The result is stored in a lookup dictionary keyed by `room_id`, and room rows are then assembled in a single Python pass with O(1) lookups per room.

```python
agg_rows = (
    db.query(
        Booking.room_id,
        func.count(Booking.id).label("cnt"),
        func.sum(Booking.price_cents).label("rev"),
    )
    .join(Room, Booking.room_id == Room.id)
    .filter(Room.org_id == admin.org_id, Booking.status == "confirmed", ...)
    .group_by(Booking.room_id)
    .all()
)
agg = {row.room_id: (row.cnt, row.rev or 0) for row in agg_rows}
```

---

### Fix 3: OOM Risk in Stats Initialization

**File:** `app/services/stats.py` (Function: `init_stats`)

**Bug:** On server startup, `init_stats` fetched every single confirmed `Booking` ORM object into memory using `.all()` and then aggregated counts and revenue in a Python loop. On a production database with thousands or millions of bookings, this caused an out-of-memory (OOM) risk at startup since entire rows were materialized as Python objects purely to compute two scalar values per room.

**Fix:** Changed `init_stats` to use SQL aggregation (`func.count`, `func.sum`) grouped by `room_id` rather than fetching full ORM objects. Only the compact aggregated scalar rows are returned and stored, making startup memory usage O(number of distinct rooms) instead of O(total bookings).

```python
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
    _stats[row.room_id] = {"count": row.cnt, "revenue": row.rev or 0}
```

---

### Fix 4: Memory Leak in Rate Limiter

**File:** `app/services/ratelimit.py` (Function: `record_and_check`)

**Bug:** After filtering expired timestamps from a user's rolling window bucket, the code always wrote the (potentially empty) list back into the `_buckets` dictionary with `_buckets[user_id] = bucket`. This meant that once a user made any booking request, their key remained in `_buckets` forever — even after all their timestamps expired — causing indefinite dictionary growth and a slow memory leak in long-running servers with many distinct users.

**Fix:** Added cleanup logic to remove the key from `_buckets` when the bucket becomes empty after trimming expired entries. The `_buckets.pop(user_id, None)` call ensures that inactive users do not leave ghost entries in memory.

```python
bucket = [t for t in bucket if t > now - _WINDOW_SECONDS]
# ... rate limit check and append ...
if bucket:
    _buckets[user_id] = bucket
else:
    _buckets.pop(user_id, None)
```

---

### Fix 5: Race Condition in Org Registration

**File:** `app/routers/auth.py` (Function: `register`)

**Bug:** A race condition existed when two users concurrently tried to register a new organization with the same name. Both requests would query for the org, both would see `org is None`, and both would call `db.add(org)` and `db.commit()`. The second commit would raise a `sqlalchemy.exc.IntegrityError` (due to the `UNIQUE` constraint on `organizations.name`), causing an unhandled 500 Internal Server Error crash.

**Fix:** Wrapped the Organization creation and commit inside a `try...except IntegrityError` block. If the commit fails due to a concurrent duplicate insertion, the transaction is rolled back, the already-committed organization is re-fetched from the database, and the registering user is correctly assigned the `member` role instead of crashing.

```python
if org is None:
    try:
        org = Organization(name=payload.org_name)
        db.add(org)
        db.commit()
        db.refresh(org)
    except IntegrityError:
        db.rollback()
        org = db.query(Organization).filter(Organization.name == payload.org_name).first()
        role = "member"
```

---

### Fix 6: Negative Hourly Rate Accepted

**File:** `app/schemas.py` (Class: `RoomCreateRequest`)

**Bug:** The `hourly_rate_cents` field in `RoomCreateRequest` was declared as a plain `int` with no bounds validation. This allowed API clients to create rooms with a negative hourly rate, which is nonsensical business logic and could cause negative `price_cents` values to be stored in bookings, corrupting revenue calculations and reports.

**Fix:** Added `Field(ge=0)` Pydantic validation to `hourly_rate_cents` to enforce that the value must be greater than or equal to zero. Pydantic now rejects negative values at the request parsing layer with a `422 Unprocessable Entity` response before any database or business logic is reached.

```python
class RoomCreateRequest(BaseModel):
    name: str
    capacity: int
    hourly_rate_cents: int = Field(ge=0)
```

---

## 2nd Iteration Audit — Additional Bugs

### Fix F1 — Hard | `app/cache.py` — No thread safety on cache dicts

**File/Line:** `app/cache.py` (all functions)

**Bug:** `_report_cache` and `_availability_cache` are plain `dict` objects mutated from multiple threads with no synchronization. In CPython, individual dict `__setitem__` / `__getitem__` are GIL-protected, but `invalidate_report` does:
```python
for key in [k for k in _report_cache if k[0] == org_id]:
    _report_cache.pop(key, None)
```
Between building the list comprehension and iterating through it, another thread can add or remove keys. More critically, if two threads call `invalidate_report` concurrently, the `pop` can race and raise `KeyError`, and building the list comprehension while another thread pops can cause the iterator to see a mutated object. In Python 3.12+ with the experimental free-threading mode (no GIL), this would be a definite data race.

**Impact:** Concurrent report invalidation (e.g. simultaneous booking creates for different rooms in the same org) risks `RuntimeError: dictionary changed size during iteration` or silently stale cache entries.

**Fix:** Added `_cache_lock = threading.Lock()` and wrapped every `get`, `set`, and `invalidate` function body in `with _cache_lock:`.

---

### Fix F2 — Medium | `app/routers/bookings.py:131-134` — Float equality for duration check

**File/Line:** `app/routers/bookings.py:131-134`

**Bug:** `duration_hours = (end - start).total_seconds() / 3600` followed by `if duration_hours != int(duration_hours)` uses floating-point division and equality. For any timedelta that is exactly N hours in seconds, Python's float division should produce an exact result, but floating-point arithmetic is not guaranteed to be exact. For example, `timedelta(hours=3).total_seconds()` = `10800.0`, and `10800.0 / 3600` = `3.0` exactly in CPython — however this relies on IEEE 754 guarantee that is implementation-specific. More importantly, this approach is semantically fragile: if the input has sub-second precision (e.g. `10800.001` seconds), the float check would pass where it should fail.

**Impact:** Edge-case durations that round to a whole number of hours in float arithmetic but are not exact whole hours may slip through validation.

**Fix:** Replaced with integer-seconds modulo check:
```python
total_seconds = int((end - start).total_seconds())
if total_seconds % 3600 != 0:
    raise AppError(...)
duration_hours = total_seconds // 3600
```
This is exact, fast, and unambiguous regardless of floating-point rounding.

---

### Fix F3 — Medium | `app/services/stats.py:31-38` — Negative revenue possible in `record_cancel`

**File/Line:** `app/services/stats.py:34-38`

**Bug:** `record_cancel` decrements `current["revenue"] - price_cents` with no lower bound. If `_stats` is ever in a state where the tracked revenue is less than the booking's price (e.g. after a restart where `init_stats` ran before all in-flight bookings committed, or if a booking was cancelled without a prior successful `record_create`), `revenue` goes negative, which is semantically invalid and corrupts `GET /rooms/{id}/stats` responses.

**Impact:** The stats endpoint can return negative `total_revenue_cents`, which is a logical error that violates Rule 14.

**Fix:** Added `max(0, ...)` clamp: `"revenue": max(0, current["revenue"] - price_cents)`.

---

### Fix F4 — Medium | `app/routers/bookings.py:121` — Stale `now` used for quota window

**File/Line:** `app/routers/bookings.py:121,154`

**Bug:** `now = datetime.utcnow()` is captured before the user lock is acquired and before `_pricing_warmup()` sleeps 120 ms. The captured `now` is then passed to `_check_quota`, which uses it to compute the 24-hour rolling window boundary: `window_end = now + timedelta(hours=24)`. Because `now` is up to 120 ms stale by the time `_check_quota` runs, the window boundary is slightly off. In practice this is a sub-second discrepancy, but it means bookings starting almost exactly 24 hours from the original request time might be incorrectly included or excluded from the quota count.

**Impact:** Quota window boundary has up to 120 ms of drift, which could cause bookings at the exact boundary of the 24-hour window to be counted or not counted erroneously.

**Fix:** Re-capture `now = datetime.utcnow()` immediately inside the lock, after `_has_conflict` (which calls `_pricing_warmup`) has returned, so the quota check uses the current clock.
