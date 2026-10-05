#!/bin/bash
# Dripp Media — Local SearXNG Launcher Helper
set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"

if ! command -v docker &> /dev/null; then
    echo "[SearXNG Helper] Docker is not installed on this system."
    echo "[SearXNG Helper] Cannot start local SearXNG. The pipeline will report NOT_CONNECTED and use the free DuckDuckGo fallback."
    exit 0
fi

echo "[SearXNG Helper] Launching local SearXNG container on http://localhost:8080..."
docker compose up -d

echo "[SearXNG Helper] Waiting for SearXNG service to respond on http://localhost:8080..."
for i in {1..10}; do
    if curl -s -f http://localhost:8080/ > /dev/null; then
        echo "[SearXNG Helper] SearXNG is CONNECTED and ready!"
        exit 0
    fi
    sleep 1
done

echo "[SearXNG Helper] SearXNG container started but not yet responding on http://localhost:8080."
