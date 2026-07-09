import decimal
import time
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient
import pytest

from app.main import app
from app.database import Base, engine, SessionLocal
from app.models import Organization, User, Room, Booking, RefundLog

client = TestClient(app)

@pytest.fixture(autouse=True)
def setup_db():
    # Re-create database schemas before each test to have a clean state
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    # Reset in-memory rate limits, revoked tokens, and per-user/room locks
    from app.services import ratelimit
    from app import auth
    from app.routers import bookings as bookings_router
    ratelimit._buckets.clear()
    auth._revoked_tokens.clear()
    auth._revoked_refresh_jtis.clear()
    bookings_router._user_locks.clear()
    bookings_router._room_locks.clear()
    bookings_router._booking_cancel_locks.clear()
    # Re-initialize stats
    db = SessionLocal()
    try:
        from app.services.stats import init_stats
        init_stats(db)
    finally:
        db.close()


def _future(hours: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).replace(
        minute=0, second=0, microsecond=0
    ).isoformat()


def test_auth_and_registration():
    # 1. Register Admin
    r1 = client.post(
        "/auth/register",
        json={"org_name": "org1", "username": "alice", "password": "password123"}
    )
    assert r1.status_code == 201
    assert r1.json()["role"] == "admin"
    assert r1.json()["username"] == "alice"

    # 2. Join Org (role should be member)
    r2 = client.post(
        "/auth/register",
        json={"org_name": "org1", "username": "bob", "password": "password123"}
    )
    assert r2.status_code == 201
    assert r2.json()["role"] == "member"

    # 3. Duplicate username within the same org should raise 409 USERNAME_TAKEN
    r3 = client.post(
        "/auth/register",
        json={"org_name": "org1", "username": "alice", "password": "newpassword"}
    )
    assert r3.status_code == 409
    assert r3.json()["code"] == "USERNAME_TAKEN"

    # 4. Same username in a different org is allowed
    r4 = client.post(
        "/auth/register",
        json={"org_name": "org2", "username": "alice", "password": "password123"}
    )
    assert r4.status_code == 201
    assert r4.json()["role"] == "admin"


def test_login_logout_and_token_rotation():
    client.post(
        "/auth/register",
        json={"org_name": "org1", "username": "alice", "password": "password123"}
    )

    # Valid Login
    r_login = client.post(
        "/auth/login",
        json={"org_name": "org1", "username": "alice", "password": "password123"}
    )
    assert r_login.status_code == 200
    tokens = r_login.json()
    assert "access_token" in tokens
    assert "refresh_token" in tokens
    assert tokens["token_type"] == "bearer"

    # Invalid Login
    r_bad = client.post(
        "/auth/login",
        json={"org_name": "org1", "username": "alice", "password": "wrong_password"}
    )
    assert r_bad.status_code == 401
    assert r_bad.json()["code"] == "INVALID_CREDENTIALS"

    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    # Logout
    r_logout = client.post("/auth/logout", headers=headers)
    assert r_logout.status_code == 200

    # Using the logged-out access token should be forbidden (401)
    r_reused = client.get("/rooms", headers=headers)
    assert r_reused.status_code == 401
    assert r_reused.json()["code"] == "UNAUTHORIZED"

    # Token Rotation (Refresh)
    r_refresh = client.post(
        "/auth/refresh",
        json={"refresh_token": tokens["refresh_token"]}
    )
    assert r_refresh.status_code == 200
    new_tokens = r_refresh.json()
    assert "access_token" in new_tokens

    # Reusing the same refresh token should be forbidden (401)
    r_refresh_reuse = client.post(
        "/auth/refresh",
        json={"refresh_token": tokens["refresh_token"]}
    )
    assert r_refresh_reuse.status_code == 401


def test_datetime_utc_normalization():
    # Register and login
    client.post(
        "/auth/register",
        json={"org_name": "org1", "username": "alice", "password": "password123"}
    )
    login = client.post(
        "/auth/login",
        json={"org_name": "org1", "username": "alice", "password": "password123"}
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    # Create room
    room = client.post(
        "/rooms",
        json={"name": "Room A", "capacity": 10, "hourly_rate_cents": 100},
        headers=headers
    )
    room_id = room.json()["id"]

    # Book with offset time (e.g. UTC+6). It should normalize to UTC.
    # Suppose we want a booking from 50 to 52 hours in future.
    # Start: +50h, End: +52h
    now_utc = datetime.now(timezone.utc)
    start_time_utc = (now_utc + timedelta(hours=50)).replace(minute=0, second=0, microsecond=0)
    end_time_utc = start_time_utc + timedelta(hours=2)

    # Format with offset (e.g., UTC+06:00)
    tz_offset = timezone(timedelta(hours=6))
    start_time_offset = start_time_utc.astimezone(tz_offset)
    end_time_offset = end_time_utc.astimezone(tz_offset)

    booking = client.post(
        "/bookings",
        json={
            "room_id": room_id,
            "start_time": start_time_offset.isoformat(),
            "end_time": end_time_offset.isoformat(),
        },
        headers=headers
    )
    assert booking.status_code == 201
    booking_data = booking.json()
    # The stored/returned start_time and end_time should end with '+00:00' (or 'Z' equivalent) and represent the UTC value
    # start_time_offset: e.g. 2026-07-11T20:00:00+06:00 -> UTC 2026-07-11T14:00:00+00:00
    assert datetime.fromisoformat(booking_data["start_time"]).astimezone(timezone.utc) == start_time_utc
    assert datetime.fromisoformat(booking_data["end_time"]).astimezone(timezone.utc) == end_time_utc


def test_booking_constraints_and_validation():
    # Register and login
    client.post(
        "/auth/register",
        json={"org_name": "org1", "username": "alice", "password": "password123"}
    )
    login = client.post(
        "/auth/login",
        json={"org_name": "org1", "username": "alice", "password": "password123"}
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    # Create room
    room = client.post(
        "/rooms",
        json={"name": "Room A", "capacity": 10, "hourly_rate_cents": 100},
        headers=headers
    )
    room_id = room.json()["id"]

    # 1. start_time in the past should fail
    past_start = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    past_end = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    r = client.post(
        "/bookings",
        json={"room_id": room_id, "start_time": past_start, "end_time": past_end},
        headers=headers
    )
    assert r.status_code == 400
    assert r.json()["code"] == "INVALID_BOOKING_WINDOW"

    # 2. Duration less than 1 hour should fail
    start = _future(10)
    end_half_hour = (datetime.fromisoformat(start) + timedelta(minutes=30)).isoformat()
    r = client.post(
        "/bookings",
        json={"room_id": room_id, "start_time": start, "end_time": end_half_hour},
        headers=headers
    )
    assert r.status_code == 400
    assert r.json()["code"] == "INVALID_BOOKING_WINDOW"

    # 3. Non-integer duration (e.g. 1.5 hours) should fail
    end_1_5_hours = (datetime.fromisoformat(start) + timedelta(hours=1, minutes=30)).isoformat()
    r = client.post(
        "/bookings",
        json={"room_id": room_id, "start_time": start, "end_time": end_1_5_hours},
        headers=headers
    )
    assert r.status_code == 400
    assert r.json()["code"] == "INVALID_BOOKING_WINDOW"

    # 4. Duration greater than 8 hours should fail
    end_9_hours = (datetime.fromisoformat(start) + timedelta(hours=9)).isoformat()
    r = client.post(
        "/bookings",
        json={"room_id": room_id, "start_time": start, "end_time": end_9_hours},
        headers=headers
    )
    assert r.status_code == 400
    assert r.json()["code"] == "INVALID_BOOKING_WINDOW"


def test_booking_conflict():
    # Register and login
    client.post(
        "/auth/register",
        json={"org_name": "org1", "username": "alice", "password": "password123"}
    )
    login = client.post(
        "/auth/login",
        json={"org_name": "org1", "username": "alice", "password": "password123"}
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    room = client.post(
        "/rooms",
        json={"name": "Room A", "capacity": 10, "hourly_rate_cents": 100},
        headers=headers
    )
    room_id = room.json()["id"]

    start = _future(10)
    end = _future(12)

    # First booking: hours 10 to 12
    r1 = client.post(
        "/bookings",
        json={"room_id": room_id, "start_time": start, "end_time": end},
        headers=headers
    )
    assert r1.status_code == 201

    # Overlapping booking: hours 11 to 13 (fails)
    r2 = client.post(
        "/bookings",
        json={"room_id": room_id, "start_time": _future(11), "end_time": _future(13)},
        headers=headers
    )
    assert r2.status_code == 409
    assert r2.json()["code"] == "ROOM_CONFLICT"

    # Back-to-back booking: hours 12 to 14 (allowed)
    r3 = client.post(
        "/bookings",
        json={"room_id": room_id, "start_time": _future(12), "end_time": _future(14)},
        headers=headers
    )
    assert r3.status_code == 201


def test_booking_quota_limit():
    # Register and login
    client.post(
        "/auth/register",
        json={"org_name": "org1", "username": "alice", "password": "password123"}
    )
    login = client.post(
        "/auth/login",
        json={"org_name": "org1", "username": "alice", "password": "password123"}
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    room = client.post(
        "/rooms",
        json={"name": "Room A", "capacity": 10, "hourly_rate_cents": 100},
        headers=headers
    )
    room_id = room.json()["id"]

    # Member can hold at most 3 confirmed bookings in the next 24 hours.
    # Let's create 3 bookings: hours 2 to 3, 4 to 5, 6 to 7.
    b1 = client.post(
        "/bookings",
        json={"room_id": room_id, "start_time": _future(2), "end_time": _future(3)},
        headers=headers
    )
    assert b1.status_code == 201

    b2 = client.post(
        "/bookings",
        json={"room_id": room_id, "start_time": _future(4), "end_time": _future(5)},
        headers=headers
    )
    assert b2.status_code == 201

    b3 = client.post(
        "/bookings",
        json={"room_id": room_id, "start_time": _future(6), "end_time": _future(7)},
        headers=headers
    )
    assert b3.status_code == 201

    # 4th booking in the next 24 hours window should violate the quota limit (409)
    b4 = client.post(
        "/bookings",
        json={"room_id": room_id, "start_time": _future(8), "end_time": _future(9)},
        headers=headers
    )
    assert b4.status_code == 409
    assert b4.json()["code"] == "QUOTA_EXCEEDED"

    # A booking *outside* the 24 hour window is allowed (e.g. +30 hours in future)
    b5 = client.post(
        "/bookings",
        json={"room_id": room_id, "start_time": _future(30), "end_time": _future(31)},
        headers=headers
    )
    assert b5.status_code == 201


def test_rate_limiting():
    client.post(
        "/auth/register",
        json={"org_name": "org1", "username": "alice", "password": "password123"}
    )
    login = client.post(
        "/auth/login",
        json={"org_name": "org1", "username": "alice", "password": "password123"}
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    # Reset rate limit bucket manually
    from app.services import ratelimit
    ratelimit._buckets.clear()

    # Create 20 requests (some might fail or succeed, but all must count towards the rate limit)
    for i in range(20):
        # We don't care about the HTTP status response here, just that we are hitting it
        client.post(
            "/bookings",
            json={"room_id": 999, "start_time": _future(10), "end_time": _future(11)},
            headers=headers
        )

    # 21st request must trigger 429 RATE_LIMITED
    r = client.post(
        "/bookings",
        json={"room_id": 999, "start_time": _future(10), "end_time": _future(11)},
        headers=headers
    )
    assert r.status_code == 429
    assert r.json()["code"] == "RATE_LIMITED"


def test_multi_tenancy_and_visibility():
    # Org 1 Admin and Member
    client.post(
        "/auth/register",
        json={"org_name": "org1", "username": "admin1", "password": "password123"}
    )
    client.post(
        "/auth/register",
        json={"org_name": "org1", "username": "member1", "password": "password123"}
    )
    login_admin1 = client.post(
        "/auth/login",
        json={"org_name": "org1", "username": "admin1", "password": "password123"}
    )
    login_member1 = client.post(
        "/auth/login",
        json={"org_name": "org1", "username": "member1", "password": "password123"}
    )
    h_admin1 = {"Authorization": f"Bearer {login_admin1.json()['access_token']}"}
    h_member1 = {"Authorization": f"Bearer {login_member1.json()['access_token']}"}

    # Org 2 Admin
    client.post(
        "/auth/register",
        json={"org_name": "org2", "username": "admin2", "password": "password123"}
    )
    login_admin2 = client.post(
        "/auth/login",
        json={"org_name": "org2", "username": "admin2", "password": "password123"}
    )
    h_admin2 = {"Authorization": f"Bearer {login_admin2.json()['access_token']}"}

    # Admin 1 creates Room 1
    room1 = client.post(
        "/rooms",
        json={"name": "Org1 Room", "capacity": 5, "hourly_rate_cents": 100},
        headers=h_admin1
    )
    room1_id = room1.json()["id"]

    # Member 1 books Room 1
    booking1 = client.post(
        "/bookings",
        json={"room_id": room1_id, "start_time": _future(10), "end_time": _future(11)},
        headers=h_member1
    )
    b1_id = booking1.json()["id"]

    # 1. Admin 2 (different org) attempts to read Room 1 (should return 404 ROOM_NOT_FOUND)
    r_room_cross = client.get(f"/rooms/{room1_id}/stats", headers=h_admin2)
    assert r_room_cross.status_code == 404
    assert r_room_cross.json()["code"] == "ROOM_NOT_FOUND"

    # 2. Admin 2 attempts to read Booking 1 (should return 404 BOOKING_NOT_FOUND)
    r_booking_cross = client.get(f"/bookings/{b1_id}", headers=h_admin2)
    assert r_booking_cross.status_code == 404
    assert r_booking_cross.json()["code"] == "BOOKING_NOT_FOUND"

    # 3. Admin 2 attempts to export Room 1 (should return 404 ROOM_NOT_FOUND)
    r_export_cross = client.get(f"/admin/export?room_id={room1_id}", headers=h_admin2)
    assert r_export_cross.status_code == 404

    # 4. Member 1 reads booking 1 (should succeed)
    r_ok = client.get(f"/bookings/{b1_id}", headers=h_member1)
    assert r_ok.status_code == 200

    # 5. Member 1 creates another booking, then logs out.
    # Admin 1 reads Member 1's booking (should succeed - admins can read any booking in their org)
    r_admin_read = client.get(f"/bookings/{b1_id}", headers=h_admin1)
    assert r_admin_read.status_code == 200


def test_pagination_and_sorting():
    client.post(
        "/auth/register",
        json={"org_name": "org1", "username": "alice", "password": "password123"}
    )
    login = client.post(
        "/auth/login",
        json={"org_name": "org1", "username": "alice", "password": "password123"}
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    room = client.post(
        "/rooms",
        json={"name": "Room A", "capacity": 10, "hourly_rate_cents": 100},
        headers=headers
    )
    room_id = room.json()["id"]

    # Bookings chronologically at +10h, +15h, +20h
    t1 = _future(10)
    t2 = _future(15)
    t3 = _future(20)

    client.post("/bookings", json={"room_id": room_id, "start_time": t2, "end_time": (datetime.fromisoformat(t2) + timedelta(hours=1)).isoformat()}, headers=headers)
    client.post("/bookings", json={"room_id": room_id, "start_time": t1, "end_time": (datetime.fromisoformat(t1) + timedelta(hours=1)).isoformat()}, headers=headers)
    client.post("/bookings", json={"room_id": room_id, "start_time": t3, "end_time": (datetime.fromisoformat(t3) + timedelta(hours=1)).isoformat()}, headers=headers)

    # Retrieve all sorted ascending by start_time
    r = client.get("/bookings?page=1&limit=10", headers=headers)
    assert r.status_code == 200
    data = r.json()
    items = data["items"]
    assert len(items) == 3
    assert items[0]["start_time"] < items[1]["start_time"] < items[2]["start_time"]

    # Test page offsets and limit
    r_p1 = client.get("/bookings?page=1&limit=2", headers=headers)
    assert len(r_p1.json()["items"]) == 2
    r_p2 = client.get("/bookings?page=2&limit=2", headers=headers)
    assert len(r_p2.json()["items"]) == 1
    assert r_p2.json()["items"][0]["id"] == items[2]["id"]


def test_refund_tiers_and_rounding():
    client.post(
        "/auth/register",
        json={"org_name": "org1", "username": "admin", "password": "password123"}
    )
    login = client.post(
        "/auth/login",
        json={"org_name": "org1", "username": "admin", "password": "password123"}
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    room = client.post(
        "/rooms",
        json={"name": "Room A", "capacity": 10, "hourly_rate_cents": 1005}, # hourly rate: 10.05
        headers=headers
    )
    room_id = room.json()["id"]

    # 1. 100% Refund: Booking starts 50 hours in future
    b1 = client.post(
        "/bookings",
        json={"room_id": room_id, "start_time": _future(50), "end_time": _future(51)},
        headers=headers
    )
    b1_id = b1.json()["id"]
    r_cancel1 = client.post(f"/bookings/{b1_id}/cancel", headers=headers)
    assert r_cancel1.status_code == 200
    assert r_cancel1.json()["refund_percent"] == 100
    assert r_cancel1.json()["refund_amount_cents"] == 1005

    # 2. 50% Refund: Booking starts 30 hours in future
    # 50% of 1005 is 502.5 -> rounds to 503 cents
    b2 = client.post(
        "/bookings",
        json={"room_id": room_id, "start_time": _future(30), "end_time": _future(31)},
        headers=headers
    )
    b2_id = b2.json()["id"]
    r_cancel2 = client.post(f"/bookings/{b2_id}/cancel", headers=headers)
    assert r_cancel2.status_code == 200
    assert r_cancel2.json()["refund_percent"] == 50
    assert r_cancel2.json()["refund_amount_cents"] == 503

    # 3. 0% Refund: Booking starts 10 hours in future
    b3 = client.post(
        "/bookings",
        json={"room_id": room_id, "start_time": _future(10), "end_time": _future(11)},
        headers=headers
    )
    b3_id = b3.json()["id"]
    r_cancel3 = client.post(f"/bookings/{b3_id}/cancel", headers=headers)
    assert r_cancel3.status_code == 200
    assert r_cancel3.json()["refund_percent"] == 0
    assert r_cancel3.json()["refund_amount_cents"] == 0

    # Cancelling already cancelled booking
    r_cancel3_again = client.post(f"/bookings/{b3_id}/cancel", headers=headers)
    assert r_cancel3_again.status_code == 409
    assert r_cancel3_again.json()["code"] == "ALREADY_CANCELLED"


def test_live_stats_and_immediate_reports():
    client.post(
        "/auth/register",
        json={"org_name": "org1", "username": "admin", "password": "password123"}
    )
    login = client.post(
        "/auth/login",
        json={"org_name": "org1", "username": "admin", "password": "password123"}
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    room = client.post(
        "/rooms",
        json={"name": "Room A", "capacity": 10, "hourly_rate_cents": 200},
        headers=headers
    )
    room_id = room.json()["id"]

    # Initial Stats & Report
    s0 = client.get(f"/rooms/{room_id}/stats", headers=headers).json()
    assert s0["total_confirmed_bookings"] == 0
    assert s0["total_revenue_cents"] == 0

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")

    rep0 = client.get(f"/admin/usage-report?from={today}&to={tomorrow}", headers=headers).json()
    assert rep0["rooms"][0]["confirmed_bookings"] == 0
    assert rep0["rooms"][0]["revenue_cents"] == 0

    # Create booking
    b = client.post(
        "/bookings",
        json={"room_id": room_id, "start_time": _future(5), "end_time": _future(7)},
        headers=headers
    )
    assert b.status_code == 201

    # Immediate Stats & Report (must immediately reflect changes, meaning report cache invalidation works)
    s1 = client.get(f"/rooms/{room_id}/stats", headers=headers).json()
    assert s1["total_confirmed_bookings"] == 1
    assert s1["total_revenue_cents"] == 400

    rep1 = client.get(f"/admin/usage-report?from={today}&to={tomorrow}", headers=headers).json()
    assert rep1["rooms"][0]["confirmed_bookings"] == 1
    assert rep1["rooms"][0]["revenue_cents"] == 400

    # Cancel booking
    client.post(f"/bookings/{b.json()['id']}/cancel", headers=headers)

    s2 = client.get(f"/rooms/{room_id}/stats", headers=headers).json()
    assert s2["total_confirmed_bookings"] == 0
    assert s2["total_revenue_cents"] == 0

    rep2 = client.get(f"/admin/usage-report?from={today}&to={tomorrow}", headers=headers).json()
    assert rep2["rooms"][0]["confirmed_bookings"] == 0
    assert rep2["rooms"][0]["revenue_cents"] == 0


def test_cross_room_quota_race():
    """Extra Bug E: Concurrent booking requests to DIFFERENT rooms for the same
    user must still respect the 3-booking-per-24h quota (cross-room race)."""
    import concurrent.futures

    client.post(
        "/auth/register",
        json={"org_name": "org1", "username": "admin", "password": "password123"}
    )
    login = client.post(
        "/auth/login",
        json={"org_name": "org1", "username": "admin", "password": "password123"}
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    # Create 4 distinct rooms
    room_ids = []
    for i in range(4):
        r = client.post(
            "/rooms",
            json={"name": f"Room {i}", "capacity": 4, "hourly_rate_cents": 100},
            headers=headers
        )
        room_ids.append(r.json()["id"])

    # Fire 4 concurrent booking requests, each targeting a different room,
    # all in the 24-hour window — only 3 should succeed.
    results = []

    def book(room_id, start_h, end_h):
        return client.post(
            "/bookings",
            json={"room_id": room_id, "start_time": _future(start_h), "end_time": _future(end_h)},
            headers=headers,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futures = [
            ex.submit(book, room_ids[0], 2, 3),
            ex.submit(book, room_ids[1], 4, 5),
            ex.submit(book, room_ids[2], 6, 7),
            ex.submit(book, room_ids[3], 8, 9),
        ]
        for f in concurrent.futures.as_completed(futures):
            results.append(f.result())

    statuses = [r.status_code for r in results]
    confirmed = statuses.count(201)
    exceeded = statuses.count(409)

    # Exactly 3 requests must succeed and at least 1 must fail with QUOTA_EXCEEDED
    assert confirmed == 3, f"Expected 3 bookings, got {confirmed}. Statuses: {statuses}"
    assert exceeded >= 1, f"Expected at least 1 QUOTA_EXCEEDED, got {exceeded}. Statuses: {statuses}"
    for r in results:
        if r.status_code == 409:
            assert r.json()["code"] == "QUOTA_EXCEEDED"

