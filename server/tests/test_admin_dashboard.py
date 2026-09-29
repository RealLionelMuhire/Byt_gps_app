"""
Route tests for app/api/admin_dashboard.py (/api/admin/dashboard/*), using
the shared conftest `client` app against in-memory SQLite. `require_admin`
is exercised for real via the caller's `role` column.

Focus: every number traces back to real payment/subscription rows —
refunds are netted, status comes from expires_at (not the stale status
column), connectivity from last_update (not Device.status), and dates are
bucketed by Africa/Kigali day.
"""

from datetime import datetime, timedelta

import pytest

from app.models.device import Device
from app.models.disbursement import Disbursement
from app.models.location import Location
from app.models.subscription import (
    Payment, Subscription, SubscriptionPlan, SubscriptionReminder, SubscriptionVehicle,
)
from app.models.trip import Trip
from app.models.user import Role, User
from app.models.vehicle import Vehicle

NOW = datetime.utcnow()


def make_user(db, clerk_id, role=Role.USER, first="Test", last="User", phone=None):
    u = User(clerk_user_id=clerk_id, email=f"{clerk_id}@example.com", first_name=first,
             last_name=last, role=role, phone_number=phone)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.fixture()
def plan(db_session):
    p = SubscriptionPlan(name="Basic", slug="basic", billing_type="recurrent", billing_model="prepaid",
                         charge_scope="per_device", price=2450, currency="RWF", duration_value=1,
                         duration_unit="month", max_devices=3, is_active=True)
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


@pytest.fixture()
def admin(db_session, current_clerk_id):
    a = make_user(db_session, "clerk_admin", role=Role.ADMIN, first="Ada", last="Admin")
    current_clerk_id["value"] = a.clerk_user_id
    return a


def pay(db, user, plan, amount, status, created_at, consumed=True, currency="RWF", tx=None):
    p = Payment(clerk_user_id=user.clerk_user_id, tx_ref=tx or f"tx-{amount}-{status}-{created_at.timestamp()}",
                plan_id=plan.id, amount=amount, currency=currency, status=status,
                created_at=created_at, verified_at=created_at + timedelta(minutes=1),
                consumed_at=created_at if consumed and status == "successful" else None)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def sub(db, user, plan, expires_at, status="active", started_at=None, created_at=None, price=2450):
    s = Subscription(clerk_user_id=user.clerk_user_id, plan_id=plan.id, status=status, price=price,
                     started_at=started_at or expires_at - timedelta(days=30), expires_at=expires_at,
                     created_at=created_at or (started_at or expires_at - timedelta(days=30)))
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


ENDPOINTS = [
    "/api/admin/dashboard/overview", "/api/admin/dashboard/revenue", "/api/admin/dashboard/payments",
    "/api/admin/dashboard/attention", "/api/admin/dashboard/clients", "/api/admin/dashboard/inventory",
    "/api/admin/dashboard/usage",
]


@pytest.mark.parametrize("path", ENDPOINTS)
def test_every_endpoint_requires_admin(client, db_session, current_clerk_id, path):
    for role in (Role.USER, Role.TECHNICIAN):
        u = make_user(db_session, f"clerk_{role.value}_{path.rsplit('/', 1)[1]}", role=role)
        current_clerk_id["value"] = u.clerk_user_id
        assert client.get(path).status_code == 403


def test_overview_revenue_nets_refunds_and_counts_real_outcomes(client, db_session, admin, plan):
    c = make_user(db_session, "clerk_c")
    refunded = pay(db_session, c, plan, 5000, "successful", NOW - timedelta(days=5))
    pay(db_session, c, plan, 2450, "successful", NOW - timedelta(days=2))
    pay(db_session, c, plan, 2450, "failed", NOW - timedelta(days=1))
    pay(db_session, c, plan, 2450, "pending", NOW - timedelta(hours=1))
    pay(db_session, c, plan, 9999, "successful", NOW - timedelta(days=90))   # outside the 30-day default
    pay(db_session, c, plan, 10, "successful", NOW - timedelta(days=3), currency="USD")
    db_session.add(Disbursement(clerk_user_id=c.clerk_user_id, phone="250780000000", tx_ref="d1",
                                amount=5000, currency="RWF", reason="refund", status="successful",
                                reference_payment_id=refunded.id, initiated_by_clerk_user_id="clerk_admin",
                                created_at=NOW - timedelta(days=4)))
    db_session.add(Disbursement(clerk_user_id=c.clerk_user_id, phone="250780000000", tx_ref="d2",
                                amount=700, currency="RWF", reason="commission", status="successful",
                                initiated_by_clerk_user_id="clerk_admin", created_at=NOW - timedelta(days=4)))
    db_session.commit()

    body = client.get("/api/admin/dashboard/overview").json()
    rev = body["revenue"]
    assert rev["gross"] == 7450
    assert rev["refunds"] == 5000
    assert rev["net"] == 2450
    assert rev["other_payouts"] == 700        # not a refund, never netted against revenue
    assert rev["successful_count"] == 3       # incl. the USD one, which is counted but not summed
    assert rev["non_rwf_count"] == 1
    assert rev["failed_count"] == 1
    assert rev["pending_count"] == 1
    assert body["attention"]["stuck_pending"] == 1
    assert body["attention"]["failed_recent"] == 1
    assert body["range"]["timezone"] == "Africa/Kigali"


def test_revenue_buckets_by_kigali_day(client, db_session, admin, plan):
    c = make_user(db_session, "clerk_c")
    # 23:30 UTC on Sep 1 is 01:30 on Sep 2 in Kigali.
    pay(db_session, c, plan, 2450, "successful", datetime(2026, 9, 1, 23, 30))
    pay(db_session, c, plan, 1000, "successful", datetime(2026, 9, 1, 12, 0))
    resp = client.get("/api/admin/dashboard/revenue?from=2026-09-01&to=2026-09-03&granularity=day")
    series = {r["bucket"]: r for r in resp.json()["series"]}
    assert list(series) == ["2026-09-01", "2026-09-02", "2026-09-03"]  # empty days are filled
    assert series["2026-09-01"]["gross"] == 1000
    assert series["2026-09-02"]["gross"] == 2450
    assert series["2026-09-03"]["gross"] == 0

    monthly = client.get("/api/admin/dashboard/revenue?from=2026-08-15&to=2026-09-30&granularity=month").json()
    assert [r["bucket"] for r in monthly["series"]] == ["2026-08-01", "2026-09-01"]
    assert monthly["series"][1]["gross"] == 3450


def test_payments_table_filters_search_refund_and_deleted_account(client, db_session, admin, plan):
    c = make_user(db_session, "clerk_c", first="Jean", last="Uwase", phone="250781112233")
    p = pay(db_session, c, plan, 5000, "successful", NOW - timedelta(days=5), tx="TX-JEAN")
    pay(db_session, c, plan, 2450, "failed", NOW - timedelta(days=1))
    ghost = User(clerk_user_id="clerk_gone", email="x", first_name="x", last_name="x")
    pay(db_session, ghost, plan, 2450, "successful", NOW - timedelta(days=3), tx="TX-GHOST")
    db_session.add(Disbursement(clerk_user_id=c.clerk_user_id, phone="1", tx_ref="d1", amount=1000,
                                currency="RWF", reason="partial", status="successful",
                                reference_payment_id=p.id, initiated_by_clerk_user_id="a"))
    db_session.commit()

    body = client.get("/api/admin/dashboard/payments").json()
    assert body["total"] == 3
    assert body["totals"]["successful_amount"] == 7450

    jean = client.get("/api/admin/dashboard/payments?q=uwase&status=successful").json()
    assert [i["tx_ref"] for i in jean["items"]] == ["TX-JEAN"]
    assert jean["items"][0]["refunded_amount"] == 1000
    assert jean["items"][0]["client_phone"] == "250781112233"

    by_phone = client.get("/api/admin/dashboard/payments?q=7811122").json()
    assert by_phone["total"] == 2

    ghost_row = client.get("/api/admin/dashboard/payments?q=TX-GHOST").json()["items"][0]
    assert ghost_row["account_deleted"] is True
    assert ghost_row["client_name"] is None

    csv_resp = client.get("/api/admin/dashboard/payments?format=csv")
    assert csv_resp.headers["content-type"].startswith("text/csv")
    lines = csv_resp.text.strip().splitlines()
    assert lines[0].startswith("id,tx_ref,created (Africa/Kigali)")
    assert len(lines) == 4
    assert "(deleted account)" in csv_resp.text


def test_clients_status_comes_from_expires_at_not_the_stale_status_column(client, db_session, admin, plan):
    lapsed = make_user(db_session, "clerk_lapsed", first="Lapsed")
    # Still "active" in the column: the 15-minute cron hasn't run yet.
    sub(db_session, lapsed, plan, NOW - timedelta(hours=2), status="active")
    rows = {r["name"]: r for r in client.get("/api/admin/dashboard/clients").json()["items"]}
    row = rows["Lapsed User"]
    assert row["status"] == "expired"
    assert row["expiry_bucket"] == "expired"
    assert row["days_remaining"] < 0
    assert row["reminder_due"] == "expired"   # cron hasn't sent the expiry notice yet


def test_clients_expiry_windows_reminders_and_cancelled(client, db_session, admin, plan):
    a = make_user(db_session, "clerk_a", first="Two")
    b = make_user(db_session, "clerk_b", first="Six")
    c = make_user(db_session, "clerk_c", first="Twenty")
    d = make_user(db_session, "clerk_d", first="Cancelled")
    make_user(db_session, "clerk_new", first="Nothing")
    make_user(db_session, "clerk_staff", role=Role.SUPER_ADMIN, first="Staff")  # no data -> not a client

    sa = sub(db_session, a, plan, NOW + timedelta(days=2))
    sub(db_session, b, plan, NOW + timedelta(days=6))
    sub(db_session, c, plan, NOW + timedelta(days=20))
    sub(db_session, d, plan, NOW + timedelta(days=20), status="cancelled")
    db_session.add(SubscriptionReminder(subscription_id=sa.id, clerk_user_id=a.clerk_user_id, stage="3d",
                                        expires_at=sa.expires_at, sent_at=NOW - timedelta(hours=3),
                                        email_sent=False, push_sent=True))
    db_session.commit()

    all_rows = client.get("/api/admin/dashboard/clients?page_size=100").json()
    names = [r["name"] for r in all_rows["items"]]
    # Default sort: soonest expiry first; clients without a date last.
    assert names[:2] == ["Two User", "Six User"]
    assert "Staff User" not in names
    assert all_rows["total"] == 5
    by_name = {r["name"]: r for r in all_rows["items"]}
    assert by_name["Cancelled User"]["status"] == "cancelled"
    assert by_name["Nothing User"]["status"] == "none"
    assert by_name["Two User"]["last_reminder"]["email_sent"] is False
    assert by_name["Two User"]["reminder_due"] is None       # 3d already sent
    assert by_name["Six User"]["reminder_due"] == "7d"       # due, not sent

    within3 = client.get("/api/admin/dashboard/clients?status=expiring&within=3").json()
    assert [r["name"] for r in within3["items"]] == ["Two User"]
    within7 = client.get("/api/admin/dashboard/clients?status=expiring&within=7").json()
    assert within7["total"] == 2
    due = client.get("/api/admin/dashboard/clients?reminder=due").json()
    assert [r["name"] for r in due["items"]] == ["Six User"]

    overview = client.get("/api/admin/dashboard/overview").json()["subscriptions"]
    assert overview["active"] == 3
    assert overview["expiring_3"] == 1
    assert overview["expiring_7"] == 2
    assert overview["expiring_14"] == 2
    assert overview["cancelled"] == 1
    assert overview["none"] == 1
    assert overview["reminders_due"] == 1

    csv_text = client.get("/api/admin/dashboard/clients?status=expiring&format=csv").text
    assert csv_text.splitlines()[0].startswith("user_id,name,email,phone,plan,status")
    assert len(csv_text.strip().splitlines()) == 3


def test_client_totals_and_detail(client, db_session, admin, plan):
    c = make_user(db_session, "clerk_c", first="Jean")
    other = make_user(db_session, "clerk_other")
    s = sub(db_session, c, plan, NOW + timedelta(days=10))
    p1 = pay(db_session, c, plan, 5000, "successful", NOW - timedelta(days=20))
    pay(db_session, c, plan, 2450, "successful", NOW - timedelta(days=1))
    pay(db_session, c, plan, 2450, "pending", NOW - timedelta(minutes=2))
    pay(db_session, other, plan, 8888, "successful", NOW - timedelta(days=1))
    db_session.add(Disbursement(clerk_user_id=c.clerk_user_id, phone="1", tx_ref="d1", amount=5000,
                                currency="RWF", reason="refund", status="successful",
                                reference_payment_id=p1.id, initiated_by_clerk_user_id="a"))
    dev1 = Device(imei="111", name="Car tracker", lifecycle="sold", user_id=c.id, status="online",
                  last_update=NOW - timedelta(minutes=1))
    dev2 = Device(imei="222", name="Moto tracker", lifecycle="sold", user_id=c.id, status="online",
                  last_update=NOW - timedelta(days=2))
    db_session.add_all([dev1, dev2])
    db_session.commit()
    v1 = Vehicle(clerk_user_id=c.clerk_user_id, device_id=dev1.id, nickname="Car", plate="RAB123A", make="T", model="C")
    v2 = Vehicle(clerk_user_id=c.clerk_user_id, device_id=dev2.id, nickname="Moto", plate="RC1", make="H", model="X")
    db_session.add_all([v1, v2])
    db_session.commit()
    db_session.add(SubscriptionVehicle(subscription_id=s.id, vehicle_id=v1.id))
    db_session.commit()

    row = client.get("/api/admin/dashboard/clients?q=jean").json()["items"][0]
    assert row["total_paid"] == 7450
    assert row["total_refunded"] == 5000
    assert row["net_paid"] == 2450
    assert row["pending_payments"] == 1
    assert row["vehicles_total"] == 2
    assert row["vehicles_covered"] == 1
    assert row["devices_total"] == 2
    assert row["devices_online"] == 1

    detail = client.get(f"/api/admin/dashboard/clients/{c.id}").json()
    assert detail["client"]["net_paid"] == 2450
    assert len(detail["payments"]) == 3
    refunded = [p for p in detail["payments"] if p["refunded_amount"]]
    assert [p["id"] for p in refunded] == [p1.id]
    covered = {v["plate"]: v["covered"] for v in detail["vehicles"]}
    assert covered == {"RAB123A": True, "RC1": False}
    conn = {d["imei"]: d["connectivity"] for d in detail["devices"]}
    assert conn == {"111": "online", "222": "offline"}
    assert detail["subscriptions"][0]["is_current"] is True
    assert client.get("/api/admin/dashboard/clients/99999").status_code == 404


def test_attention_lists_stuck_failed_and_paid_but_not_activated(client, db_session, admin, plan):
    c = make_user(db_session, "clerk_c")
    pay(db_session, c, plan, 2450, "pending", NOW - timedelta(minutes=5))           # too fresh
    pay(db_session, c, plan, 2450, "pending", NOW - timedelta(hours=2), tx="STUCK")
    pay(db_session, c, plan, 2450, "failed", NOW - timedelta(days=40))              # too old
    pay(db_session, c, plan, 2450, "failed", NOW - timedelta(days=2), tx="FAILED")
    pay(db_session, c, plan, 2450, "successful", NOW - timedelta(hours=3), consumed=False, tx="ORPHAN")
    pay(db_session, c, plan, 2450, "successful", NOW - timedelta(hours=3), tx="FINE")
    items = client.get("/api/admin/dashboard/attention").json()["items"]
    assert {(i["tx_ref"], i["reason"]) for i in items} == {
        ("STUCK", "stuck_pending"), ("FAILED", "failed"), ("ORPHAN", "paid_not_activated"),
    }


def test_inventory_connectivity_ignores_stale_tcp_status_flag(client, db_session, admin, plan):
    owner = make_user(db_session, "clerk_owner", first="Owner")
    db_session.add_all([
        Device(imei="1", name="Live", lifecycle="sold", user_id=owner.id, status="online",
               last_update=NOW - timedelta(minutes=2)),
        # Still flagged online, but nothing received for an hour.
        Device(imei="2", name="Ghost", lifecycle="sold", user_id=owner.id, status="online",
               last_update=NOW - timedelta(hours=1)),
        Device(imei="3", name="Boxed", lifecycle="registered", status="offline"),
        Device(imei="4", name="Shelf", lifecycle="in_stock", status="offline", last_update=NOW - timedelta(days=9)),
    ])
    db_session.commit()
    rows = {r["imei"]: r for r in client.get("/api/admin/dashboard/inventory").json()["items"]}
    assert rows["1"]["connectivity"] == "online"
    assert rows["2"]["connectivity"] == "offline"
    assert rows["2"]["offline_minutes"] >= 59
    assert rows["3"]["connectivity"] == "never"
    assert rows["1"]["owner_name"] == "Owner User"
    assert rows["4"]["owner_name"] is None

    inv = client.get("/api/admin/dashboard/overview").json()["inventory"]
    assert inv == {"total": 4, "registered": 1, "in_stock": 1, "sold": 2, "online": 1,
                   "never_connected": 1, "offline": 2}

    unsold = client.get("/api/admin/dashboard/inventory?lifecycle=in_stock").json()
    assert [r["imei"] for r in unsold["items"]] == ["4"]
    csv_text = client.get("/api/admin/dashboard/inventory?format=csv").text
    assert len(csv_text.strip().splitlines()) == 5


def test_usage_counts_points_and_trips_in_range(client, db_session, admin, plan):
    owner = make_user(db_session, "clerk_owner", first="Owner")
    dev = Device(imei="1", name="Car", lifecycle="sold", user_id=owner.id, last_update=NOW)
    db_session.add(dev)
    db_session.commit()
    for i in range(3):
        db_session.add(Location(device_id=dev.id, latitude=-1.9, longitude=30.0, gps_valid=i != 0,
                                timestamp=NOW - timedelta(hours=i + 1)))
    db_session.add(Location(device_id=dev.id, latitude=-1.9, longitude=30.0, gps_valid=True,
                            timestamp=NOW - timedelta(days=60)))
    db_session.add(Trip(device_id=dev.id, user_id=owner.id, name="t", start_time=NOW - timedelta(hours=2),
                        end_time=NOW - timedelta(hours=1), total_distance_km=12.5))
    db_session.commit()
    body = client.get("/api/admin/dashboard/usage").json()
    d = body["devices"][0]
    assert (d["points"], d["valid_points"], d["trips"], d["distance_km"]) == (3, 2, 1, 12.5)
    assert body["clients"][0]["points"] == 3
    overview = client.get("/api/admin/dashboard/overview").json()["usage"]
    assert overview == {"points": 3, "active_devices": 1, "trips": 1, "distance_km": 12.5}


def test_bad_parameters_are_rejected(client, db_session, admin):
    assert client.get("/api/admin/dashboard/clients?sort=password").status_code == 422
    assert client.get("/api/admin/dashboard/overview?from=2026-09-10&to=2026-09-01").status_code == 422
    assert client.get("/api/admin/dashboard/revenue?granularity=hour").status_code == 422
