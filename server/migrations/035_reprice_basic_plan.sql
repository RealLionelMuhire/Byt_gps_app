-- Migration 035: Re-price the "basic" subscription plan to 2450 RWF/month
-- Run with: psql $DATABASE_URL -f migrations/035_reprice_basic_plan.sql
--
-- Safe for existing subscribers: Subscription.price is snapshotted at
-- purchase time (see app/models/subscription.py), so this only changes what
-- future purchases/renewals of the "basic" plan cost — it does not alter
-- any already-active subscription's stored price.

UPDATE subscription_plans
SET price = 2450, updated_at = NOW()
WHERE slug = 'basic';
