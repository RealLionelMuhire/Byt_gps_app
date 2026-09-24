"""
Tests for plan entitlements (app/services/entitlements.py, migration 047):
feature resolution from the device owner's plan, log-only vs enforce
modes, limits, the /api/me/entitlements endpoint, and admin plan-feature
management.

Uses the `client`/`db_session`/`current_clerk_id` fixtures from conftest.py.
"""

import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.core.config import settings
from app.models.entitlement import EntitlementCheckLog, Feature, PlanFeature
from app.models.geofence import Geofence
from app.models.subscription import Subscription
from app.models.user import Role
from app.services.entitlements import FEATURES, PLAN_COLUMN_FEATURES, require_feature, seed_plan_features
from tests.test_geofences_api import VALID_BODY, make_device, make_user
from tests.test_plan_expiry_freshness import make_plan

MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "047_add_feature_entitlements.sql"


@pytest.fixture
def catalog(db_session):
    for i, f in enumerate(FEATURES):
        db_session.add(Feature(key=f.key, name=f.name, description=f.description, group=f.group,
                               kind=f.kind, unit=f.unit, sort_order=i * 10))
    db_session.commit()


@pytest.fixture
def mode(monkeypatch):
    def set_mode(value):
        monkeypatch.setattr(settings, "ENTITLEMENT_MODE", value)
    set_mode("log")
    return set_mode


def full_plan(db, slug="basic", max_devices=3, **plan_kwargs):
    plan = make_plan(db, slug, max_devices=max_devices, **plan_kwargs)
    seed_plan_features(db, plan)
    db.commit()
    return plan


def subscribe(db, user, plan, *, expired=False):
    now = datetime.utcnow()
    db.add(Subscription(
        clerk_user_id=user.clerk_user_id, plan_id=plan.id, status="active", price=plan.price,
        started_at=now - timedelta(days=40), expires_at=now + timedelta(days=-1 if expired else 20),
    ))
    db.commit()


def remove_feature(db, plan, key):
    db.query(PlanFeature).filter_by(plan_id=plan.id, feature_key=key).delete()
    db.commit()


def log_rows(db):
    return db.query(EntitlementCheckLog).all()


# --- Catalog consistency -----------------------------------------------------


def test_migration_seed_matches_the_code_registry():
    seeded = set(re.findall(r"^\s+\('([a-z_.]+)',", MIGRATION.read_text(), re.M))
    assert seeded == {f.key for f in FEATURES}


def test_unknown_feature_key_is_rejected_at_import_time():
    with pytest.raises(ValueError):
        require_feature("not.a.feature")


def test_seed_plan_features_grants_every_catalog_feature_but_vehicles_max(db_session, catalog):
    plan = full_plan(db_session)
    keys = {r.feature_key for r in db_session.query(PlanFeature).filter_by(plan_id=plan.id)}
    assert keys == {f.key for f in FEATURES} - PLAN_COLUMN_FEATURES


# --- Log-only mode -----------------------------------------------------------


def test_log_mode_lets_the_request_through_and_counts_the_would_be_denial(
    client, db_session, current_clerk_id, catalog, mode,
):
    user = make_user(db_session, "clerk_nopay")
    current_clerk_id["value"] = user.clerk_user_id

    assert client.get("/api/geofences").status_code == 200
    assert client.get("/api/geofences").status_code == 200

    (row,) = log_rows(db_session)
    assert (row.owner_user_id, row.feature_key, row.reason) == (user.id, "geofences.enabled", "no_subscription")
    assert row.route == "GET /api/geofences"
    assert row.mode == "log"
    assert row.count == 2


def test_log_mode_records_nothing_for_an_entitled_user(client, db_session, current_clerk_id, catalog, mode):
    user = make_user(db_session, "clerk_paid")
    subscribe(db_session, user, full_plan(db_session))
    current_clerk_id["value"] = user.clerk_user_id

    assert client.get("/api/geofences").status_code == 200
    assert log_rows(db_session) == []


def test_off_mode_checks_nothing(client, db_session, current_clerk_id, catalog, mode):
    mode("off")
    user = make_user(db_session, "clerk_nopay")
    current_clerk_id["value"] = user.clerk_user_id

    assert client.get("/api/geofences").status_code == 200
    assert log_rows(db_session) == []


# --- Enforce mode ------------------------------------------------------------


@pytest.mark.parametrize("setup, reason", [
    ("none", "no_subscription"),
    ("expired", "subscription_expired"),
    ("not_in_plan", "not_in_plan"),
])
def test_enforce_mode_denies_with_a_structured_402(
    client, db_session, current_clerk_id, catalog, mode, setup, reason,
):
    mode("enforce")
    user = make_user(db_session, "clerk_user")
    if setup != "none":
        plan = full_plan(db_session)
        subscribe(db_session, user, plan, expired=setup == "expired")
        if setup == "not_in_plan":
            remove_feature(db_session, plan, "geofences.enabled")
    current_clerk_id["value"] = user.clerk_user_id

    resp = client.get("/api/geofences")

    assert resp.status_code == 402
    detail = resp.json()["detail"]
    assert detail["code"] == "feature_not_in_plan"
    assert detail["feature"] == "geofences.enabled"
    assert detail["reason"] == reason
    assert detail["message"]
    assert log_rows(db_session)[0].mode == "enforce"


def test_enforce_mode_allows_an_entitled_user(client, db_session, current_clerk_id, catalog, mode):
    mode("enforce")
    user = make_user(db_session, "clerk_paid")
    subscribe(db_session, user, full_plan(db_session))
    current_clerk_id["value"] = user.clerk_user_id

    assert client.get("/api/geofences").status_code == 200


def test_device_routes_check_the_owners_plan_not_the_callers(
    client, db_session, current_clerk_id, catalog, mode,
):
    mode("enforce")
    owner = make_user(db_session, "clerk_owner")
    plan = full_plan(db_session)
    subscribe(db_session, owner, plan)
    remove_feature(db_session, plan, "alerts.history")
    device = make_device(db_session, owner, "111111111111111")

    current_clerk_id["value"] = owner.clerk_user_id
    resp = client.get(f"/api/locations/{device.id}/alarms")
    assert resp.status_code == 402
    assert log_rows(db_session)[0].owner_user_id == owner.id

    # An admin acting on the same device isn't blocked by the owner's plan.
    admin = make_user(db_session, "clerk_admin", role=Role.ADMIN)
    current_clerk_id["value"] = admin.clerk_user_id
    assert client.get(f"/api/locations/{device.id}/alarms").status_code == 200


def test_missing_device_falls_through_to_the_routes_own_404(
    client, db_session, current_clerk_id, catalog, mode,
):
    mode("enforce")
    user = make_user(db_session, "clerk_user")
    current_clerk_id["value"] = user.clerk_user_id

    assert client.get("/api/locations/9999/alarms").status_code == 404


def test_checks_fail_open_when_entitlements_cant_be_resolved(
    client, db_session, current_clerk_id, catalog, mode, monkeypatch,
):
    # e.g. deployed before migration 047 was applied: the check must not
    # take every gated route down with it.
    from sqlalchemy.exc import ProgrammingError
    import app.services.entitlements as entitlements_module

    def broken(db, owner):
        raise ProgrammingError("SELECT ...", {}, Exception('relation "plan_features" does not exist'))

    monkeypatch.setattr(entitlements_module, "resolve_entitlements", broken)
    mode("enforce")
    user = make_user(db_session, "clerk_user")
    current_clerk_id["value"] = user.clerk_user_id

    assert client.get("/api/geofences").status_code == 200
    assert client.post("/api/geofences", json=VALID_BODY).status_code == 201


# --- Limits and in-handler checks --------------------------------------------


def set_limit(db, plan, key, value):
    row = db.query(PlanFeature).filter_by(plan_id=plan.id, feature_key=key).one()
    row.limit_value = value
    db.commit()


def test_zone_limit_blocks_the_zone_past_the_limit_in_enforce_mode(
    client, db_session, current_clerk_id, catalog, mode,
):
    mode("enforce")
    user = make_user(db_session, "clerk_user")
    plan = full_plan(db_session)
    subscribe(db_session, user, plan)
    set_limit(db_session, plan, "geofences.max_zones", 1)
    current_clerk_id["value"] = user.clerk_user_id

    assert client.post("/api/geofences", json=VALID_BODY).status_code == 201
    resp = client.post("/api/geofences", json=VALID_BODY)

    assert resp.status_code == 402
    assert resp.json()["detail"]["reason"] == "over_limit"
    assert resp.json()["detail"]["limit"] == 1
    assert db_session.query(Geofence).count() == 1


def test_zone_limit_only_logs_in_log_mode(client, db_session, current_clerk_id, catalog, mode):
    user = make_user(db_session, "clerk_user")
    plan = full_plan(db_session)
    subscribe(db_session, user, plan)
    set_limit(db_session, plan, "geofences.max_zones", 0)
    current_clerk_id["value"] = user.clerk_user_id

    assert client.post("/api/geofences", json=VALID_BODY).status_code == 201
    (row,) = log_rows(db_session)
    assert (row.feature_key, row.reason) == ("geofences.max_zones", "over_limit")


def test_polygon_zones_need_their_own_feature(client, db_session, current_clerk_id, catalog, mode):
    mode("enforce")
    user = make_user(db_session, "clerk_user")
    plan = full_plan(db_session)
    subscribe(db_session, user, plan)
    remove_feature(db_session, plan, "geofences.polygon")
    current_clerk_id["value"] = user.clerk_user_id
    polygon = {"name": "Yard", "shape_type": "polygon",
               "points": [{"lat": -1.9, "lng": 30.0}, {"lat": -1.9, "lng": 30.1}, {"lat": -1.8, "lng": 30.1}]}

    assert client.post("/api/geofences", json=polygon).status_code == 402
    assert client.post("/api/geofences", json=VALID_BODY).status_code == 201


# --- GET /api/me/entitlements ------------------------------------------------


def test_my_entitlements_reports_plan_features_and_vehicle_limit(
    client, db_session, current_clerk_id, catalog, mode,
):
    user = make_user(db_session, "clerk_user")
    plan = full_plan(db_session, max_devices=3)
    subscribe(db_session, user, plan)
    remove_feature(db_session, plan, "commands.fuel_cut")
    current_clerk_id["value"] = user.clerk_user_id

    body = client.get("/api/me/entitlements").json()

    assert body["mode"] == "log"
    assert body["status"] == "active"
    assert body["plan"]["slug"] == "basic"
    assert body["features"]["vehicles.max"] == {"enabled": True, "limit": 3}
    assert body["features"]["commands.fuel_cut"]["enabled"] is False
    assert body["features"]["tracking.live"]["enabled"] is True
    assert set(body["features"]) == {f.key for f in FEATURES}


def test_my_entitlements_without_a_plan_grants_nothing(client, db_session, current_clerk_id, catalog, mode):
    user = make_user(db_session, "clerk_user")
    current_clerk_id["value"] = user.clerk_user_id

    body = client.get("/api/me/entitlements").json()

    assert body["status"] == "none"
    assert body["plan"] is None
    assert not any(g["enabled"] for g in body["features"].values())


# --- Admin plan-feature management -------------------------------------------


@pytest.fixture
def admin_client(client, db_session, current_clerk_id):
    admin = make_user(db_session, "clerk_admin", role=Role.ADMIN)
    current_clerk_id["value"] = admin.clerk_user_id
    return client


def test_admin_lists_the_catalog_in_order(admin_client, catalog):
    body = admin_client.get("/api/admin/features").json()

    assert [f["key"] for f in body] == [f.key for f in FEATURES]
    vehicles = next(f for f in body if f["key"] == "vehicles.max")
    assert vehicles["backed_by_plan_column"] is True


def test_admin_replaces_a_plans_feature_set(admin_client, db_session, catalog):
    plan = full_plan(db_session)

    resp = admin_client.put(f"/api/admin/plans/{plan.id}/features", json={"features": [
        {"key": "tracking.live"},
        {"key": "geofences.enabled"},
        {"key": "geofences.max_zones", "limit": 3},
    ]})

    assert resp.status_code == 200, resp.text
    assert [(f["key"], f["limit"]) for f in resp.json()["features"]] == [
        ("tracking.live", None), ("geofences.enabled", None), ("geofences.max_zones", 3),
    ]
    assert db_session.query(PlanFeature).filter_by(plan_id=plan.id).count() == 3


@pytest.mark.parametrize("item, message", [
    ({"key": "nope"}, "Unknown feature"),
    ({"key": "vehicles.max", "limit": 5}, "max_devices"),
    ({"key": "tracking.live", "limit": 5}, "doesn't take a limit"),
    ({"key": "geofences.max_zones", "limit": -1}, "negative"),
])
def test_admin_plan_feature_validation(admin_client, db_session, catalog, item, message):
    plan = full_plan(db_session)
    before = db_session.query(PlanFeature).filter_by(plan_id=plan.id).count()

    resp = admin_client.put(f"/api/admin/plans/{plan.id}/features", json={"features": [item]})

    assert resp.status_code == 400
    assert message in resp.json()["detail"]
    assert db_session.query(PlanFeature).filter_by(plan_id=plan.id).count() == before


def test_non_admin_cannot_manage_plan_features(client, db_session, current_clerk_id, catalog):
    plan = full_plan(db_session)
    user = make_user(db_session, "clerk_user")
    current_clerk_id["value"] = user.clerk_user_id

    assert client.get("/api/admin/features").status_code == 403
    assert client.put(f"/api/admin/plans/{plan.id}/features", json={"features": []}).status_code == 403
