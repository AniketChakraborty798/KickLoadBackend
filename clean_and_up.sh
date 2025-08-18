#!/bin/bash

echo "🧹 Cleaning Docker system (containers, images, networks, volumes)..."

# Step 1: Stop all containers cleanly
docker stop $(docker ps -q)

# Step 2: Prune Docker containers, images, networks, builders (except volumes)
docker container prune -f
docker image prune -af
docker network prune -f
docker builder prune -af

# Step 3: Clean only orphaned volumes (safe — does NOT delete named like mongo_data)
echo "🧽 Cleaning dangling/orphaned Docker volumes..."
docker volume prune -f

# Step 4: Clean build cache
echo "🧼 Cleaning Docker build cache older than 24h..."
docker builder prune -af --filter "until=24h"

# Step 5: Clean scratch/temp directories left by JMeter (but KEEP mongo data)
echo "🗑️ Cleaning /tmp/jmeter scratch dirs..."
sudo rm -rf /tmp/jmeter/*

# Step 6: Reclaim EC2 space — clean system logs, pip cache
echo "🧹 Cleaning OS-level logs and pip cache..."
sudo journalctl --vacuum-time=3d
sudo rm -rf /var/log/*.gz /var/log/*.[0-9] /var/log/*/*gz /var/log/*-????????
sudo rm -rf ~/.cache/pip /root/.cache/pip

# Step 7: (Optional) clean unused apt files (if apt exists — safe to ignore error)
sudo apt-get clean 2>/dev/null || true

# Step 8: Start Docker Compose stack
echo "🚀 Rebuilding and starting Docker Compose stack..."
docker-compose up --build -d

# Step 9: Show final disk usage
echo "💾 Disk usage after cleanup:"
df -hT | grep '/$'

