# Dripp Media International Leads - Production Containerfile
# Python 3.11 Slim Base
FROM python:3.11-slim

# Set environment invariants
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

# Install runtime and build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Create application directory
WORKDIR /app

# Install Python dependencies first for optimal Docker layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy codebase
COPY . .

# Create non-root application user and persistent data directories
RUN groupadd -g 1000 appgroup && \
    useradd -u 1000 -g appgroup -s /bin/bash -m appuser && \
    mkdir -p /app/data && \
    chown -R appuser:appgroup /app

# Switch to unprivileged user
USER appuser

# Expose HTTP port
EXPOSE 8000

# Container healthcheck against canonical backend health endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:${PORT:-8080}/api/system/health || exit 1

# Start FastAPI application via Uvicorn with proxy headers enabled for HTTPS ingress
CMD exec uvicorn server:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers
