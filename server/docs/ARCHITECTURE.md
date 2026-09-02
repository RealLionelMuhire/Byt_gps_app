# Backend Architecture: Company → Subscriptions → Billing

## Overview

This document describes the current backend architecture after the refactor from **user-centric** to **company-centric** ownership. GPS devices, subscriptions, and billing are now organized around companies, not individual users.

---

## Entity Relationship Diagram

```
┌──────────────┐       ┌──────────────────┐       ┌──────────────┐
│    users     │       │   memberships    │       │  companies   │
├──────────────┤       ├──────────────────┤       ├──────────────┤
│ id           │──┐    │ id               │    ┌──│ id           │
│ clerk_user_id│  └───▶│ user_id          │    │  │ name         │
│ email        │       │ company_id       │◀───┘  │ is_company   │
│ first_name   │       │ company_role     │       │ created_at   │
│ last_name    │       │ created_at       │       │ updated_at   │
│ role (global)│       └──────────────────┘       └──────┬───────┘
│ onboarding_* │                                        │
│ created_at   │       ┌──────────────────┐             │
│ updated_at   │       │     devices      │             │
└──────────────┘       ├──────────────────┤             │
                       │ id               │             │
                       │ imei             │             │
                       │ company_id       │◀────────────┘
                       │ lifecycle        │
                       │ status           │
                       │ plan_id          │──┐
                       │ pairing_pin      │  │
                       │ marker_icon      │  │
                       └──────────────────┘  │
                                             │
                       ┌──────────────────┐  │
                       │subscription_plans│  │
                       ├──────────────────┤  │
                       │ id               │◀─┘
                       │ slug             │
                       │ name             │
                       │ billing_type     │  prepaid | postpaid
                       │ pricing_model    │  per_device | flat
                       │ price            │
                       │ currency         │
                       │ duration_value   │
                       │ duration_unit    │
                       │ min_devices      │
                       │ max_devices      │
                       │ is_active        │
                       └────────┬─────────┘
                                │
                       ┌────────▼─────────┐
                       │  subscriptions   │
                       ├──────────────────┤
                       │ id               │
                       │ company_id       │──▶ companies
                       │ clerk_user_id    │──▶ users (legacy)
                       │ plan_id          │──▶ subscription_plans.slug
                       │ billing_type     │  snapshot from plan
                       │ pricing_model    │  snapshot from plan
                       │ device_count     │  devices at subscribe time
                       │ status           │  active | expired | cancelled
                       │ price            │  unit price
                       │ amount_due       │  total to pay
                       │ payment_status   │  pending | paid | overdue
                       │ due_date         │  postpaid only
                       │ started_at       │
                       │ expires_at       │  calculated from plan
                       │ created_at       │
                       │ updated_at       │
                       └──────────────────┘
```

---

## What Changed (Before → After)

### Before: User-Centric

```
User
  ├── Device (device.user_id = user.id)
  ├── Subscription (subscription.clerk_user_id = user.id)
  └── Payment (payment.clerk_user_id = user.id)
```

- GPS devices belonged to individual users
- Subscriptions were per-user
- No company concept
- No multi-tenant support

### After: Company-Centric

```
Company
  ├── Memberships (who's in it + company role)
  ├── Devices (what GPS it owns)
  ├── Subscription (what plan it's on)
  └── Payments (payment history)
```

- GPS devices belong to companies
- Subscriptions are per-company
- Users access devices through company membership
- Multi-tenant from day one

---

## Core Entities

### User

| Field | Type | Purpose |
|---|---|---|
| `id` | int | Primary key |
| `clerk_user_id` | string | Clerk auth ID (from JWT) |
| `email` | string | Email address |
| `first_name` | string | First name |
| `last_name` | string | Last name |
| `role` | enum | Global role: SUPER_ADMIN, ADMIN, TECHNICIAN, USER |
| `onboarding_step` | int | Current onboarding progress (0-9) |
| `onboarding_complete` | bool | Whether onboarding is done |

**Key rule:** User identity always comes from JWT token, never from request body.

### Company

| Field | Type | Purpose |
|---|---|---|
| `id` | int | Primary key |
| `name` | string | Company name (or user's name for solo workspace) |
| `is_company` | bool | false = solo workspace, true = named company |
| `created_at` | datetime | Creation timestamp |

**Auto-creation:** During onboarding, a company is automatically created from the user's first/last name.

### Membership

| Field | Type | Purpose |
|---|---|---|
| `id` | int | Primary key |
| `user_id` | int | FK → users.id |
| `company_id` | int | FK → companies.id |
| `company_role` | enum | OWNER or USER |
| `created_at` | datetime | When they joined |

**Roles:**
- `OWNER` — can manage company, invite/remove members, change roles
- `USER` — can view/manage devices the OWNER grants access to

**Global vs Company roles:**
- Global role (`user.role`) controls platform access (admin, technician, etc.)
- Company role (`membership.company_role`) controls per-company access
- They are independent — changing one doesn't affect the other

### Device

| Field | Type | Purpose |
|---|---|---|
| `id` | int | Primary key |
| `imei` | string | Device IMEI (unique) |
| `name` | string | Device name |
| `company_id` | int | FK → companies.id (NULL = unassigned inventory) |
| `lifecycle` | enum | registered → in_stock → sold |
| `status` | enum | online | offline (independent of lifecycle) |
| `plan_id` | int | FK → subscription_plans.id (billing scheme) |
| `pairing_pin` | string | Secret PIN for customer self-pairing |

**Lifecycle states:**

| State | Meaning |
|---|---|
| `registered` | Admin added IMEI, never connected via TCP |
| `in_stock` | First TCP handshake received, proven functional |
| `sold` | Assigned to a company (`company_id` is set) |

**Transitions:**

```
registered → in_stock   (automatic: first TCP handshake)
in_stock   → sold       (assign/pair/transfer to company)
sold       → in_stock   (unassign — back to inventory)
```

### SubscriptionPlan

| Field | Type | Purpose |
|---|---|---|
| `id` | int | Primary key |
| `slug` | string | Identifier sent by mobile app (trial, basic, fleet) |
| `name` | string | Display name |
| `billing_type` | enum | prepaid or postpaid |
| `pricing_model` | enum | per_device or flat |
| `price` | float | Unit price |
| `currency` | string | Currency code (default: RWF) |
| `duration_value` | int | Duration amount |
| `duration_unit` | string | day, week, month, year |
| `min_devices` | int | Minimum devices required |
| `max_devices` | int | Maximum devices allowed (NULL = unlimited) |
| `is_active` | bool | Whether plan is available for purchase |

**Pricing models:**

| Model | Calculation | Example |
|---|---|---|
| `flat` | price × 1 | 15,000 RWF covers all devices |
| `per_device` | price × device_count | 5,000 RWF × 7 devices = 35,000 |

**Billing types:**

| Type | Payment timing | Due date |
|---|---|---|
| `prepaid` | Pay before use | None (already paid) |
| `postpaid` | Pay after use | = expires_at |

### Subscription

| Field | Type | Purpose |
|---|---|---|
| `id` | int | Primary key |
| `company_id` | int | FK → companies.id |
| `clerk_user_id` | string | Who subscribed (legacy compat) |
| `plan_id` | string | Slug of the plan |
| `billing_type` | enum | Snapshot from plan at subscription time |
| `pricing_model` | enum | Snapshot from plan at subscription time |
| `device_count_snapshot` | int | Devices at subscription time |
| `status` | enum | active, expired, cancelled |
| `price` | float | Unit price from plan |
| `amount_due` | float | Total: price × devices (per_device) or just price (flat) |
| `payment_status` | enum | pending, paid, overdue, partial |
| `due_date` | datetime | When payment is due (postpaid only) |
| `started_at` | datetime | When subscription started |
| `expires_at` | datetime | Calculated: started_at + plan.duration |

**Status flow:**

```
pending → active → expired
            ↓
        cancelled
```

---

## API Endpoints

### Auth & User Management

| Endpoint | Method | Auth | Description |
|---|---|---|---|
| `/api/auth/sync` | POST | JWT | Sync user from Clerk (creates or updates) |
| `/api/auth/me` | GET | JWT | Get current user profile |
| `/api/auth/user/{id}` | GET | Admin | Get any user profile |
| `/api/auth/users/{id}/role` | PUT | Admin | Change user's global role |
| `/api/auth/admin-create-user` | POST | Admin | Admin creates a user |
| `/api/auth/push-token` | PUT | JWT | Update Expo push token |

### Company Management

| Endpoint | Method | Auth | Description |
|---|---|---|---|
| `/api/companies` | POST | JWT | Create company (becomes OWNER) |
| `/api/admin/companies` | POST | Admin | Create company (no membership) |
| `/api/companies/search` | GET | JWT | Search by name or member |
| `/api/companies/{id}/invite-codes` | POST | OWNER/Admin | Generate invite code |
| `/api/companies/{id}/join` | POST | JWT | Join via invite code |
| `/api/companies/{id}/members` | POST | Admin | Add member directly |
| `/api/companies/{id}/members/{uid}` | DELETE | OWNER/Admin | Remove member |
| `/api/companies/{id}/members/{uid}/role` | PUT | OWNER/Admin | Change role |

### Device Management

| Endpoint | Method | Auth | Description |
|---|---|---|---|
| `/api/devices` | POST | Admin | Create device (inventory) |
| `/api/devices` | GET | JWT | List devices (filtered by company) |
| `/api/devices/{id}` | GET | JWT | Get device details |
| `/api/devices/{id}/assign` | POST | Admin | Assign to company |
| `/api/devices/{id}/unassign` | POST | Admin | Remove from company |
| `/api/devices/{id}/transfer` | POST | Admin | Move between companies |
| `/api/devices/pair` | POST | JWT | Customer self-pairing |

### Subscription & Billing

| Endpoint | Method | Auth | Description |
|---|---|---|---|
| `/api/subscription-plans` | GET | JWT | List active plans |
| `/api/subscription-plans` | POST | Admin | Create plan |
| `/api/subscription-plans/{id}` | PUT | Admin | Update plan |
| `/api/subscription-plans/{id}` | DELETE | Admin | Deactivate plan |
| `/api/subscriptions` | POST | JWT | Activate plan (self-service) |
| `/api/subscriptions/upgrade` | POST | JWT | Upgrade plan |
| `/api/subscriptions/billing/{company_id}` | GET | JWT | Billing summary |
| `/api/admin/subscriptions` | POST | Admin | Create subscription for company |
| `/api/admin/subscriptions` | GET | Admin | List all subscriptions |
| `/api/admin/subscriptions/{id}` | PUT | Admin | Update subscription |
| `/api/admin/subscriptions/{id}` | DELETE | Admin | Cancel subscription |

### Location & Tracking

| Endpoint | Method | Auth | Description |
|---|---|---|---|
| `/api/devices/{id}/locations` | GET | JWT | Location history |
| `/api/devices/{id}/nearby` | GET | JWT | Nearby devices |
| `/ws/fleet` | WS | JWT | Real-time fleet updates |

---

## Billing Flow

### 1. Admin Creates Subscription Plan (Menu)

```
Admin → POST /api/subscription-plans
  { name, slug, billing_type, pricing_model, price, duration, min/max_devices }
```

### 2. Admin Assigns Subscription to Company

```
Admin → POST /api/admin/subscriptions
  { company_id, plan_slug, price? }

System:
  1. Count company's devices
  2. Enforce min/max device limits
  3. Calculate amount_due:
     - per_device: price × device_count
     - flat: price
  4. Set payment_status:
     - prepaid: "paid"
     - postpaid: "pending"
  5. Set due_date:
     - postpaid: expires_at
     - prepaid: null
  6. Store device_count_snapshot
```

### 3. Billing Summary

```
GET /api/subscriptions/billing/{company_id}

Response:
{
  "companyId": 5,
  "companyName": "Acme Fleet",
  "activeSubscription": {
    "planId": "fleet",
    "billingType": "prepaid",
    "pricingModel": "per_device",
    "unitPrice": 5000,
    "amountDue": 35000,
    "paymentStatus": "paid",
    "deviceCountAtSubscription": 7,
    "currentDeviceCount": 7,
    "expiresAt": "2026-10-02T14:00:00"
  },
  "totalDevices": 7,
  "billingSummary": {
    "currentAmountDue": 35000,
    "recalculatedAmount": 35000,
    "hasActiveSubscription": true,
    "paymentStatus": "paid"
  },
  "paymentHistory": [...]
}
```

### 4. Payment Integration (Future)

```
Payment received → POST /api/webhooks/payments
  → Verify transaction
  → Update subscription.payment_status = "paid"
  → Record in payments table
```

---

## Migration Path

Run migrations in order:

| Migration | Purpose |
|---|---|
| 024 | Add company_id to devices |
| 025 | Add company_id to subscriptions/payments |
| 026 | Add created_by to subscriptions |
| 027 | Revert subscriptions to user-based (kept for compat) |
| 028 | Add pricing_model to subscription_plans |
| 029 | Add company billing fields to subscriptions |
| 030 | Auto-create companies from users, move GPS to company |

---

## Security Principles

1. **User identity from JWT, never body** — `clerk_user_id` comes from `Depends(require_auth)`
2. **Company access via membership** — `require_device_access` checks membership table
3. **Role separation** — global roles (admin) vs company roles (owner/user) are independent
4. **Admin overrides** — global admin can do anything, regardless of company role
5. **Single-use invite codes** — with optional TTL, prevent reuse
6. **Fresh pairing PIN on unassign** — security reset when device changes hands
