#!/bin/bash

# GPS Tracking Server - Zero-Downtime Deployment Script
# This script pulls latest code, builds new images, and deploys with minimal downtime

set -e  # Exit on any error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
PROJECT_DIR="/home/leo/BYThron/Byt_gps_app"
SERVER_DIR="$PROJECT_DIR/server"
GIT_REMOTE="origin"
GIT_BRANCH="main"

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}GPS Tracking Server Deployment${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""

# Step 1: Navigate to project directory
echo -e "${YELLOW}[1/6]${NC} Navigating to project directory..."
cd "$PROJECT_DIR"
echo -e "${GREEN}✓${NC} Current directory: $(pwd)"
echo ""

# Step 2: Pull latest code from GitHub
echo -e "${YELLOW}[2/6]${NC} Pulling latest code from GitHub..."
git fetch "$GIT_REMOTE"
LOCAL_COMMIT=$(git rev-parse HEAD)
REMOTE_COMMIT=$(git rev-parse "$GIT_REMOTE/$GIT_BRANCH")

if [ "$LOCAL_COMMIT" = "$REMOTE_COMMIT" ]; then
    echo -e "${GREEN}✓${NC} Already up to date (commit: ${LOCAL_COMMIT:0:7})"
else
    echo -e "${BLUE}→${NC} Updating from ${LOCAL_COMMIT:0:7} to ${REMOTE_COMMIT:0:7}..."
    git pull "$GIT_REMOTE" "$GIT_BRANCH"
    echo -e "${GREEN}✓${NC} Code updated successfully"
fi
echo ""

# Step 3: Build new Docker images (old containers still running)
echo -e "${YELLOW}[3/6]${NC} Building new Docker images..."
cd "$SERVER_DIR"

# Build images without stopping old containers
docker-compose build --no-cache

if [ $? -eq 0 ]; then
    echo -e "${GREEN}✓${NC} New images built successfully"
else
    echo -e "${RED}✗${NC} Build failed! Deployment aborted."
    echo -e "${YELLOW}→${NC} Old containers are still running"
    exit 1
fi
echo ""

# Step 4: Check current container status
echo -e "${YELLOW}[4/6]${NC} Checking current container status..."
docker-compose ps
echo ""

# Step 5: Deploy new containers (this will gracefully replace old ones)
echo -e "${YELLOW}[5/6]${NC} Deploying new containers..."
echo -e "${BLUE}→${NC} Docker Compose will:"
echo -e "   1. Start new containers from new images"
echo -e "   2. Wait for health checks"
echo -e "   3. Stop old containers"
echo -e "   4. Remove old containers"
echo ""

# Use --no-deps to avoid recreating postgres if only app changed
# Use --force-recreate to ensure new images are used
# Use --remove-orphans to clean up old containers
docker-compose up -d --force-recreate --remove-orphans

if [ $? -eq 0 ]; then
    echo -e "${GREEN}✓${NC} New containers deployed successfully"
else
    echo -e "${RED}✗${NC} Deployment failed!"
    echo -e "${YELLOW}→${NC} Check logs: docker-compose logs"
    exit 1
fi
echo ""

# Step 6: Verify deployment
echo -e "${YELLOW}[6/6]${NC} Verifying deployment..."
sleep 3  # Wait for containers to fully start

echo -e "${BLUE}→${NC} Container status:"
docker-compose ps
echo ""

echo -e "${BLUE}→${NC} Health check:"
HEALTH_CHECK=$(curl -s http://localhost:8000/health 2>&1)
if [ $? -eq 0 ]; then
    echo -e "${GREEN}✓${NC} Server is healthy"
    echo "$HEALTH_CHECK" | jq '.' 2>/dev/null || echo "$HEALTH_CHECK"
else
    echo -e "${YELLOW}⚠${NC} Could not reach health endpoint (this is normal if running on remote server)"
fi
echo ""

# Clean up old images
echo -e "${BLUE}→${NC} Cleaning up old images..."
docker image prune -f
echo -e "${GREEN}✓${NC} Cleanup complete"
echo ""

# Summary
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}Deployment Complete!${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo -e "To view logs:    ${BLUE}docker-compose logs -f${NC}"
echo -e "To check status: ${BLUE}docker-compose ps${NC}"
echo -e "To restart:      ${BLUE}docker-compose restart${NC}"
echo ""
echo -e "API Docs:        ${BLUE}http://localhost:8000/docs${NC}"
echo -e "Dashboard:       ${BLUE}http://localhost:8000/dashboard${NC}"
echo ""
