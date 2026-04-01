# Browser Worker Service Dockerfile
# Includes both Python (FastAPI) and Node.js (Playwright)

FROM mcr.microsoft.com/playwright:v1.41.0-jammy

WORKDIR /app

# Install Python and required system libraries
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.11 \
    python3-pip \
    python3.11-venv \
    python3.11-dev \
    libffi-dev \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Create symlinks for python
RUN ln -sf /usr/bin/python3.11 /usr/bin/python3 && \
    ln -sf /usr/bin/python3 /usr/bin/python

# Create non-root user with home directory (for npm cache)
RUN groupadd -r appuser && useradd -r -g appuser -G audio,video -m appuser

# Set up Python virtual environment
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install Python dependencies
COPY browser-worker/requirements.txt /app/browser-worker/
# Install cffi first to ensure the C extension is built correctly
RUN pip install --no-cache-dir cffi
RUN pip install --no-cache-dir -r /app/browser-worker/requirements.txt

# Copy Node.js project files and install dependencies
COPY package.json package-lock.json* ./
RUN npm ci --omit=dev

# Copy TypeScript config and source
COPY tsconfig.json ./
COPY src/scraping/ ./src/scraping/
COPY src/auth/ ./src/auth/
COPY scripts/ ./scripts/

# Copy browser worker FastAPI app
COPY browser-worker/ ./browser-worker/

# Create data directories
RUN mkdir -p /app/data/raw /app/data/sessions /app/data/processed

# Set permissions
RUN chown -R appuser:appuser /app

# Set Python path
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# Switch to non-root user
USER appuser

# Expose port
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=30s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

# Run with uvicorn
CMD ["sh", "-c", "uvicorn browser-worker.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
