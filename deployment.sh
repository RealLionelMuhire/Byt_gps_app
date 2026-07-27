#!/bin/bash

# GPS Tracking Server - Zero-Downtime Deployment Script
# Pulls exact branch state from GitHub, builds Docker image, and deploys.
#
# Usage:
#   ./deployment.sh
#   ./deployment.sh --no-cache

set -e

# ==============================
# Options
# ==============================

NO_CACHE=""

if [[ "$1" == "--no-cache" ]]; then
    NO_CACHE="--no-cache"
    echo "Running with --no-cache (clean build)"
fi


# ==============================
# Colors
# ==============================

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'


# ==============================
# Configuration
# ==============================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PROJECT_DIR="$SCRIPT_DIR"
SERVER_DIR="$PROJECT_DIR/server"

GIT_REMOTE="origin"

# CHANGE THIS:
# Production = main
# Test      = test
GIT_BRANCH=$(git rev-parse --abbrev-ref HEAD)


# ==============================
# Docker Compose detection
# ==============================

if docker compose version &>/dev/null; then
    DOCKER_COMPOSE="docker compose"
elif docker-compose version &>/dev/null; then
    DOCKER_COMPOSE="docker-compose"
else
    echo -e "${RED}✗${NC} Docker Compose not found"
    exit 1
fi


echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}GPS Tracking Server Deployment${NC}"
echo -e "${BLUE}Branch: ${GIT_BRANCH}${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""


# ==============================
# Step 1
# ==============================

echo -e "${YELLOW}[1/6]${NC} Navigating to project directory..."

cd "$PROJECT_DIR"

echo -e "${GREEN}✓${NC} Current directory: $(pwd)"
echo ""


# ==============================
# Step 2
# Git synchronization
# ==============================

echo -e "${YELLOW}[2/6]${NC} Syncing code from GitHub..."

git fetch "$GIT_REMOTE"

REMOTE_BRANCH="$GIT_REMOTE/$GIT_BRANCH"

if ! git show-ref --verify --quiet refs/remotes/$REMOTE_BRANCH; then
    echo -e "${RED}✗${NC} Remote branch $REMOTE_BRANCH does not exist"
    exit 1
fi


LOCAL_COMMIT=$(git rev-parse HEAD)
REMOTE_COMMIT=$(git rev-parse "$REMOTE_BRANCH")


if [ "$LOCAL_COMMIT" = "$REMOTE_COMMIT" ]; then

    echo -e "${GREEN}✓${NC} Already up to date"
    echo "Commit: ${LOCAL_COMMIT:0:7}"

else

    echo -e "${BLUE}→${NC} Updating:"
    echo "Local : ${LOCAL_COMMIT:0:7}"
    echo "Remote: ${REMOTE_COMMIT:0:7}"

    # Deployment servers must match GitHub exactly
    git reset --hard "$REMOTE_BRANCH"

    # Remove generated/untracked files
    git clean -fd

    echo -e "${GREEN}✓${NC} Code synchronized"

fi

echo ""


# ==============================
# Step 3
# Docker build
# ==============================

echo -e "${YELLOW}[3/6]${NC} Building Docker image..."

cd "$SERVER_DIR"

$DOCKER_COMPOSE build $NO_CACHE


if [ $? -eq 0 ]; then

    echo -e "${GREEN}✓${NC} Docker build successful"

else

    echo -e "${RED}✗${NC} Docker build failed"
    exit 1

fi

echo ""


# ==============================
# Step 4
# Current containers
# ==============================

echo -e "${YELLOW}[4/6]${NC} Checking current containers..."

$DOCKER_COMPOSE ps

echo ""


# ==============================
# Step 5
# Deployment
# ==============================

echo -e "${YELLOW}[5/6]${NC} Deploying containers..."

echo -e "${BLUE}→${NC} Starting new containers..."

$DOCKER_COMPOSE up -d \
    --force-recreate \
    --remove-orphans


if [ $? -eq 0 ]; then

    echo -e "${GREEN}✓${NC} Deployment successful"

else

    echo -e "${RED}✗${NC} Deployment failed"
    exit 1

fi

echo ""


# ==============================
# Step 6
# Verification
# ==============================

echo -e "${YELLOW}[6/6]${NC} Verifying deployment..."

sleep 5


echo ""
echo -e "${BLUE}→${NC} Container status:"
$DOCKER_COMPOSE ps


echo ""
echo -e "${BLUE}→${NC} Health check:"


HEALTH_CHECK=$(curl -s http://localhost:8000/health 2>&1)


if [ $? -eq 0 ]; then

    echo -e "${GREEN}✓${NC} Server is healthy"

    echo "$HEALTH_CHECK" | jq '.' 2>/dev/null || echo "$HEALTH_CHECK"

else

    echo -e "${YELLOW}⚠${NC} Health endpoint unavailable"

fi


echo ""


# ==============================
# Cleanup
# ==============================

echo -e "${BLUE}→${NC} Cleaning unused Docker images..."

docker image prune -f


echo -e "${GREEN}✓${NC} Cleanup complete"


echo ""

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}Deployment Complete!${NC}"
echo -e "${GREEN}========================================${NC}"

echo ""

echo "Branch:        $GIT_BRANCH"
echo "Project:       $PROJECT_DIR"
echo ""

echo -e "Logs:          ${BLUE}$DOCKER_COMPOSE logs -f${NC}"
echo -e "Status:        ${BLUE}$DOCKER_COMPOSE ps${NC}"
echo -e "Restart:       ${BLUE}$DOCKER_COMPOSE restart${NC}"

echo ""

echo -e "Clean rebuild: ${BLUE}./deployment.sh --no-cache${NC}"

echo ""

echo -e "API Docs:      ${BLUE}http://localhost:8000/docs${NC}"
echo -e "Dashboard:     ${BLUE}http://localhost:8000/dashboard${NC}"

echo ""
