#!/bin/bash
#
# Test trip endpoints against the live server.
# Prerequisites: User synced, device with location data, device assigned to user.
#
# Usage: ./test_trips.sh [BASE_URL] [DEVICE_ID] [CLERK_USER_ID]
#

BASE_URL="${1:-http://164.92.212.186:8000}"
DEVICE_ID="${2:-1}"
CLERK_USER_ID="${3:-user_trip_test}"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

echo "=============================================="
echo " Trip API Tests"
echo " Server:      $BASE_URL"
echo " Device ID:   $DEVICE_ID"
echo " Clerk User:  $CLERK_USER_ID"
echo "=============================================="

# ── Step 1: Sync user (required for trips) ─────────────────────────────
echo ""
echo -e "${CYAN}[1] Syncing test user...${NC}"
SYNC_RESP=$(curl -s -X POST "$BASE_URL/api/auth/sync" \
  -H "Content-Type: application/json" \
  -d "{\"clerk_user_id\": \"$CLERK_USER_ID\", \"email\": \"trip-test@bythron.com\", \"name\": \"Trip Test User\"}")
echo "$SYNC_RESP" | python3 -m json.tool 2>/dev/null || echo "$SYNC_RESP"
echo ""

# ── Step 2: Assign device to user (if not already) ────────────────────
echo -e "${CYAN}[2] Assigning device $DEVICE_ID to user...${NC}"
ASSIGN_RESP=$(curl -s -w "\n%{http_code}" -X POST "$BASE_URL/api/devices/$DEVICE_ID/assign" \
  -H "X-Clerk-User-Id: $CLERK_USER_ID")
HTTP=$(echo "$ASSIGN_RESP" | tail -1)
BODY=$(echo "$ASSIGN_RESP" | sed '$d')
if [ "$HTTP" -ge 200 ] && [ "$HTTP" -lt 300 ]; then
  echo -e "     ${GREEN}OK${NC}"
  echo "$BODY" | python3 -m json.tool 2>/dev/null | sed 's/^/     /'
else
  echo -e "     ${YELLOW}HTTP $HTTP (device may already be assigned)${NC}"
  echo "$BODY" | python3 -m json.tool 2>/dev/null | sed 's/^/     /' || echo "     $BODY"
fi
echo ""

# ── Step 3: Get location history to find valid time range ─────────────
echo -e "${CYAN}[3] Fetching location history for device $DEVICE_ID...${NC}"
# Default: last 24 hours (no start/end = server default)
HISTORY=$(curl -s "$BASE_URL/api/locations/$DEVICE_ID/history?limit=100")
COUNT=$(echo "$HISTORY" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('total_points',0))" 2>/dev/null)
if [ -z "$COUNT" ] || [ "$COUNT" = "0" ]; then
  echo -e "     ${RED}No location data. Create a trip with a device that has GPS history.${NC}"
  echo "     You can still test list/get (will be empty) and create (will fail with 400)."
  echo ""
  # Use placeholder times - create will fail
  START_TIME="2025-01-01T00:00:00"
  END_TIME="2025-01-01T01:00:00"
else
  echo -e "     ${GREEN}Found $COUNT location points${NC}"
  # Get first and last timestamp from locations
  START_TIME=$(echo "$HISTORY" | python3 -c "
import sys,json
d=json.load(sys.stdin)
locs=d.get('locations',[])
if not locs: sys.exit(1)
# History is desc, so last is oldest
ts=[x['timestamp'] for x in locs]
print(min(ts))
" 2>/dev/null)
  END_TIME=$(echo "$HISTORY" | python3 -c "
import sys,json
d=json.load(sys.stdin)
locs=d.get('locations',[])
if not locs: sys.exit(1)
ts=[x['timestamp'] for x in locs]
print(max(ts))
" 2>/dev/null)
  if [ -z "$START_TIME" ] || [ -z "$END_TIME" ]; then
    START_TIME="2025-01-01T00:00:00"
    END_TIME="2025-01-01T01:00:00"
  else
    echo "     Time range: $START_TIME → $END_TIME"
  fi
fi
echo ""

# ── Step 4: Create trip ──────────────────────────────────────────────
echo -e "${CYAN}[4] Creating trip...${NC}"
CREATE_BODY="{\"device_id\": $DEVICE_ID, \"name\": \"Test Trip\", \"start_time\": \"$START_TIME\", \"end_time\": \"$END_TIME\"}"
CREATE_RESP=$(curl -s -w "\n%{http_code}" -X POST "$BASE_URL/api/trips" \
  -H "Content-Type: application/json" \
  -H "X-Clerk-User-Id: $CLERK_USER_ID" \
  -d "$CREATE_BODY")
HTTP=$(echo "$CREATE_RESP" | tail -1)
BODY=$(echo "$CREATE_RESP" | sed '$d')
if [ "$HTTP" -ge 200 ] && [ "$HTTP" -lt 300 ]; then
  echo -e "     ${GREEN}HTTP $HTTP OK${NC}"
  echo "$BODY" | python3 -m json.tool 2>/dev/null | sed 's/^/     /'
  TRIP_ID=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id',''))" 2>/dev/null)
else
  echo -e "     ${RED}HTTP $HTTP${NC}"
  echo "$BODY" | python3 -m json.tool 2>/dev/null | sed 's/^/     /' || echo "     $BODY"
  TRIP_ID=""
fi
echo ""

# ── Step 5: List trips ───────────────────────────────────────────────
echo -e "${CYAN}[5] Listing trips...${NC}"
LIST_RESP=$(curl -s -w "\n%{http_code}" "$BASE_URL/api/trips" \
  -H "X-Clerk-User-Id: $CLERK_USER_ID")
HTTP=$(echo "$LIST_RESP" | tail -1)
BODY=$(echo "$LIST_RESP" | sed '$d')
if [ "$HTTP" -ge 200 ] && [ "$HTTP" -lt 300 ]; then
  echo -e "     ${GREEN}HTTP $HTTP OK${NC}"
  echo "$BODY" | python3 -m json.tool 2>/dev/null | sed 's/^/     /'
else
  echo -e "     ${RED}HTTP $HTTP${NC}"
  echo "$BODY" | sed 's/^/     /'
fi
echo ""

# ── Step 6: Get trip detail (if we created one) ───────────────────────
if [ -n "$TRIP_ID" ]; then
  echo -e "${CYAN}[6] Getting trip $TRIP_ID detail...${NC}"
  DETAIL_RESP=$(curl -s -w "\n%{http_code}" "$BASE_URL/api/trips/$TRIP_ID" \
    -H "X-Clerk-User-Id: $CLERK_USER_ID")
  HTTP=$(echo "$DETAIL_RESP" | tail -1)
  BODY=$(echo "$DETAIL_RESP" | sed '$d')
  if [ "$HTTP" -ge 200 ] && [ "$HTTP" -lt 300 ]; then
    echo -e "     ${GREEN}HTTP $HTTP OK${NC}"
    echo "$BODY" | python3 -m json.tool 2>/dev/null | head -40 | sed 's/^/     /'
    echo "     ... (route geometry truncated)"
  else
    echo -e "     ${RED}HTTP $HTTP${NC}"
    echo "$BODY" | sed 's/^/     /'
  fi
  echo ""

  # ── Step 7: Delete trip ───────────────────────────────────────────
  echo -e "${CYAN}[7] Deleting trip $TRIP_ID...${NC}"
  DEL_RESP=$(curl -s -w "\n%{http_code}" -X DELETE "$BASE_URL/api/trips/$TRIP_ID" \
    -H "X-Clerk-User-Id: $CLERK_USER_ID")
  HTTP=$(echo "$DEL_RESP" | tail -1)
  if [ "$HTTP" = "204" ]; then
    echo -e "     ${GREEN}HTTP 204 OK (deleted)${NC}"
  else
    echo -e "     ${RED}HTTP $HTTP${NC}"
  fi
fi

# ── Step 8: Trip settings ──────────────────────────────────────────────
echo ""
echo -e "${CYAN}[8] Getting trip settings...${NC}"
SETTINGS_RESP=$(curl -s -w "\n%{http_code}" "$BASE_URL/api/trips/settings" \
  -H "X-Clerk-User-Id: $CLERK_USER_ID")
HTTP=$(echo "$SETTINGS_RESP" | tail -1)
BODY=$(echo "$SETTINGS_RESP" | sed '$d')
if [ "$HTTP" -ge 200 ] && [ "$HTTP" -lt 300 ]; then
  echo -e "     ${GREEN}HTTP $HTTP OK${NC}"
  echo "$BODY" | python3 -m json.tool 2>/dev/null | sed 's/^/     /'
else
  echo -e "     ${RED}HTTP $HTTP${NC}"
  echo "$BODY" | sed 's/^/     /'
fi

echo ""
echo -e "${CYAN}[9] Updating trip settings (stop_splits=30min)...${NC}"
UPDATE_RESP=$(curl -s -w "\n%{http_code}" -X PUT "$BASE_URL/api/trips/settings" \
  -H "Content-Type: application/json" \
  -H "X-Clerk-User-Id: $CLERK_USER_ID" \
  -d '{"stop_splits_trip_after_minutes": 30}')
HTTP=$(echo "$UPDATE_RESP" | tail -1)
BODY=$(echo "$UPDATE_RESP" | sed '$d')
if [ "$HTTP" -ge 200 ] && [ "$HTTP" -lt 300 ]; then
  echo -e "     ${GREEN}HTTP $HTTP OK${NC}"
  echo "$BODY" | python3 -m json.tool 2>/dev/null | sed 's/^/     /'
else
  echo -e "     ${RED}HTTP $HTTP${NC}"
  echo "$BODY" | sed 's/^/     /'
fi

echo ""
echo -e "${CYAN}[10] Getting suggested trips...${NC}"
SUGGESTED_RESP=$(curl -s -w "\n%{http_code}" "$BASE_URL/api/trips/suggested?device_id=$DEVICE_ID" \
  -H "X-Clerk-User-Id: $CLERK_USER_ID")
HTTP=$(echo "$SUGGESTED_RESP" | tail -1)
BODY=$(echo "$SUGGESTED_RESP" | sed '$d')
if [ "$HTTP" -ge 200 ] && [ "$HTTP" -lt 300 ]; then
  echo -e "     ${GREEN}HTTP $HTTP OK${NC}"
  COUNT=$(echo "$BODY" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null)
  echo "     Found $COUNT suggested trip(s)"
  echo "$BODY" | python3 -m json.tool 2>/dev/null | head -35 | sed 's/^/     /'
  echo "     ..."
else
  echo -e "     ${RED}HTTP $HTTP${NC}"
  echo "$BODY" | sed 's/^/     /'
fi

echo ""
echo "=============================================="
echo " Trip tests complete"
echo "=============================================="
