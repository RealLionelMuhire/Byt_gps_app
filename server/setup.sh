#!/bin/bash
# Quick Setup Script for GPS Tracking Server on 164.92.212.186

set -e

echo "🚀 GPS Tracking Server - Quick Setup"
echo "====================================="
echo ""

# Check if running as root
if [ "$EUID" -ne 0 ]; then 
    echo "⚠️  Please run as root or with sudo"
    exit 1
fi

echo "📦 Installing dependencies..."
apt update
apt install -y docker.io docker-compose postgresql-client ufw

echo ""
echo "🔥 Configuring firewall..."
ufw allow 8000/tcp comment "GPS Tracking HTTP API"
ufw allow 7018/tcp comment "GPS Tracker TCP"
ufw --force enable

echo ""
echo "📝 Creating environment file..."
if [ ! -f .env ]; then
    cp .env.example .env
    
    # Generate secret key
    SECRET_KEY=$(openssl rand -hex 32)
    sed -i "s/your-secret-key-change-this-in-production-use-openssl-rand-hex-32/$SECRET_KEY/" .env
    
    echo "✅ Environment file created"
else
    echo "⚠️  .env already exists, skipping..."
fi

echo ""
echo "🐳 Starting Docker containers..."
docker-compose up -d

echo ""
echo "⏳ Waiting for services to start..."
sleep 10

echo ""
echo "🔍 Checking service status..."
docker-compose ps

echo ""
echo "✅ Setup Complete!"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📊 Service URLs:"
echo "   HTTP API:     http://164.92.212.186:8000"
echo "   API Docs:     http://164.92.212.186:8000/docs"
echo "   Health Check: http://164.92.212.186:8000/health"
echo "   TCP Port:     164.92.212.186:7018"
echo ""
echo "🔧 Configure your GPS tracker:"
echo "   Send SMS: SERVER#164.92.212.186#7018#"
echo "   Or use:   sudo ./gps_config.py"
echo ""
echo "📋 Useful commands:"
echo "   View logs:    docker-compose logs -f"
echo "   Stop server:  docker-compose down"
echo "   Restart:      docker-compose restart"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
