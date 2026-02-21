#!/bin/bash
#
# Test all device command endpoints against the live server.
# Usage: ./test_commands.sh [BASE_URL] [DEVICE_ID]
#

BASE_URL="${1:-http://164.92.212.186:8000}"
DEVICE_ID="${2:-1}"
PASS=0
FAIL=0
TOTAL=0

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

run_test() {
    local method="$1"
    local path="$2"
    local body="$3"
    local label="$4"

    TOTAL=$((TOTAL + 1))
    printf "\n${CYAN}[%02d] %s${NC}\n" "$TOTAL" "$label"
    printf "     %s %s\n" "$method" "$path"
    if [ -n "$body" ]; then
        printf "     Body: %s\n" "$body"
    fi

    if [ "$method" = "GET" ]; then
        response=$(curl -s -w "\n%{http_code}" "$BASE_URL$path")
    else
        response=$(curl -s -w "\n%{http_code}" -X "$method" \
            -H "Content-Type: application/json" \
            -d "$body" \
            "$BASE_URL$path")
    fi

    http_code=$(echo "$response" | tail -1)
    resp_body=$(echo "$response" | sed '$d')

    if [ "$http_code" -ge 200 ] && [ "$http_code" -lt 300 ]; then
        printf "     ${GREEN}HTTP %s  OK${NC}\n" "$http_code"
        PASS=$((PASS + 1))
    elif [ "$http_code" -eq 409 ]; then
        printf "     ${YELLOW}HTTP %s  Device not connected (expected if offline)${NC}\n" "$http_code"
        PASS=$((PASS + 1))
    else
        printf "     ${RED}HTTP %s  FAILED${NC}\n" "$http_code"
        FAIL=$((FAIL + 1))
    fi

    echo "$resp_body" | python3 -m json.tool 2>/dev/null | head -20 | sed 's/^/     /'
    sleep 1
}

echo "=============================================="
echo " GPS Command Endpoint Tests"
echo " Server:    $BASE_URL"
echo " Device ID: $DEVICE_ID"
echo "=============================================="

# ── Prerequisites ──────────────────────────────────────────────

run_test GET "/" "" \
    "Root — server info"

run_test GET "/health" "" \
    "Health check — TCP connections"

run_test GET "/api/devices/" "" \
    "List devices"

run_test GET "/api/devices/$DEVICE_ID/status" "" \
    "Device status"

run_test GET "/api/devices/$DEVICE_ID/diagnostics?samples=5" "" \
    "Device diagnostics"

# ── Raw command ────────────────────────────────────────────────

run_test POST "/api/devices/$DEVICE_ID/command" \
    '{"command": "STATUS#"}' \
    "Raw command — STATUS#"

# ── Query endpoints ────────────────────────────────────────────

run_test POST "/api/devices/$DEVICE_ID/query/status" "" \
    "Query status"

run_test POST "/api/devices/$DEVICE_ID/query/location" "" \
    "Query location"

# ── Alarm toggles (enable) ─────────────────────────────────────

run_test POST "/api/devices/$DEVICE_ID/alarm/vibration" \
    '{"enabled": true}' \
    "Enable vibration alarm"

run_test POST "/api/devices/$DEVICE_ID/alarm/lowbattery" \
    '{"enabled": true}' \
    "Enable low battery alarm"

run_test POST "/api/devices/$DEVICE_ID/alarm/acc" \
    '{"enabled": true}' \
    "Enable ACC alarm"

run_test POST "/api/devices/$DEVICE_ID/alarm/overspeed" \
    '{"enabled": true, "speed_kmh": 120}' \
    "Enable overspeed alarm (120 km/h)"

run_test POST "/api/devices/$DEVICE_ID/alarm/displacement" \
    '{"enabled": true, "radius_meters": 200}' \
    "Enable displacement alarm (200m)"

run_test POST "/api/devices/$DEVICE_ID/alarm/sos" \
    '{"enabled": true}' \
    "Enable SOS alarm"

# ── Alarm toggles (disable) ────────────────────────────────────

run_test POST "/api/devices/$DEVICE_ID/alarm/vibration" \
    '{"enabled": false}' \
    "Disable vibration alarm"

run_test POST "/api/devices/$DEVICE_ID/alarm/lowbattery" \
    '{"enabled": false}' \
    "Disable low battery alarm"

run_test POST "/api/devices/$DEVICE_ID/alarm/acc" \
    '{"enabled": false}' \
    "Disable ACC alarm"

run_test POST "/api/devices/$DEVICE_ID/alarm/overspeed" \
    '{"enabled": false}' \
    "Disable overspeed alarm"

run_test POST "/api/devices/$DEVICE_ID/alarm/displacement" \
    '{"enabled": false}' \
    "Disable displacement alarm"

run_test POST "/api/devices/$DEVICE_ID/alarm/sos" \
    '{"enabled": false}' \
    "Disable SOS alarm"

# ── Fuel control ───────────────────────────────────────────────

run_test POST "/api/devices/$DEVICE_ID/fuel/cut" "" \
    "Cut fuel (IMMOBILIZE)"

run_test POST "/api/devices/$DEVICE_ID/fuel/restore" "" \
    "Restore fuel"

echo ""
echo "=============================================="
printf " Results: ${GREEN}%d passed${NC}, ${RED}%d failed${NC}, %d total\n" "$PASS" "$FAIL" "$TOTAL"
echo "=============================================="
echo ""
