#!/bin/bash

echo "♻️ Light Docker cleanup (no image deletion, no rebuild)..."

# Stop running containers
docker stop $(docker ps -q) 2>/dev/null || true

# Prune stopped containers and unused networks
docker container prune -f
docker network prune -f

# Prune **dangling** (unnamed) volumes — safe for mongo_data
docker volume prune -f

# Clean only /tmp/jmeter (safe)
echo "🧽 Cleaning JMeter scratch dirs..."
sudo rm -rf /tmp/jmeter/*

# Just restart existing containers (no --build)
echo "🚀 Starting Docker Compose stack (no rebuild)..."
docker-compose up --build -d

# Show disk usage
echo "💾 Disk usage after light cleanup:"
df -hT | grep '/$'

