# Orchestrator Service Dockerfile
# Multi-stage build for smaller final image

# =============================================================================
# Stage 1: Build dependencies
# =============================================================================
FROM python:3.11-slim as builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# =============================================================================
# Stage 2: Runtime
# =============================================================================
FROM python:3.11-slim

WORKDIR /app

# Create non-root user for security
RUN groupadd -r appuser && useradd -r -g appuser appuser

# Copy installed packages from builder
COPY --from=builder /root/.local /home/appuser/.local
ENV PATH=/home/appuser/.local/bin:$PATH

# Copy application code
COPY src/ ./src/

# Create data directories with proper permissions
RUN mkdir -p /app/data/processed /app/data/state /app/data/sessions && \
    chown -R appuser:appuser /app/data

# Set Python path
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# Switch to non-root user
USER appuser

# Expose port (Cloud Run uses $PORT, default 8080)
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

# Run with uvicorn
# Cloud Run sets PORT environment variable
CMD ["sh", "-c", "uvicorn src.api.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
