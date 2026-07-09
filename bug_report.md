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
