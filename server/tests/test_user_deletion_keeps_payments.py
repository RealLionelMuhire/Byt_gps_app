"""Clerk user.deleted must never erase the financial record: payments (and
the subscriptions they funded) outlive the user row, the active
subscription is ended, and a refunded payment no longer makes the whole
deletion fail on the disbursements FK."""

import asyncio
from datetime import datetime, timedelta

from app.api.webhooks import handle_user_deleted
from app.models.device import Device
from app.models.disbursement import Disbursement
from app.models.subscription import Payment, Subscription, SubscriptionPlan
from app.models.user import User


def test_user_deletion_keeps_payments_and_cancels_subscription(db_session):
    plan = SubscriptionPlan(name="Basic", slug="basic", price=2450, currency="RWF")
    user = User(clerk_user_id="clerk_gone", email="g@example.com", first_name="G", last_name="G")
    db_session.add_all([plan, user])
    db_session.commit()
    device = Device(imei="1", name="d", lifecycle="sold", user_id=user.id)
    sub = Subscription(clerk_user_id="clerk_gone", plan_id=plan.id, status="active", price=2450,
                       expires_at=datetime.utcnow() + timedelta(days=10))
    db_session.add_all([device, sub])
    db_session.commit()
    payment = Payment(clerk_user_id="clerk_gone", tx_ref="tx1", plan_id=plan.id, amount=2450,
                      currency="RWF", status="successful", subscription_id=sub.id)
    db_session.add(payment)
    db_session.commit()
    db_session.add(Disbursement(clerk_user_id="clerk_gone", phone="1", tx_ref="d1", amount=2450,
                                reason="refund", status="successful", reference_payment_id=payment.id,
                                initiated_by_clerk_user_id="a"))
    db_session.commit()

    asyncio.run(handle_user_deleted("clerk_gone", db_session))

    db_session.expire_all()
    assert db_session.query(User).filter_by(clerk_user_id="clerk_gone").first() is None
    assert db_session.query(Payment).filter_by(tx_ref="tx1").one().amount == 2450
    assert db_session.query(Subscription).filter_by(id=sub.id).one().status == "cancelled"
    freed = db_session.query(Device).filter_by(imei="1").one()
    assert (freed.user_id, freed.lifecycle) == (None, "in_stock")
