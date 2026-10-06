# Multi-stage build for production-ready Docker image
FROM python:3.11-slim as builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    wget \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# Production stage
FROM python:3.11-slim

WORKDIR /app

# Install wget for health checks
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN useradd -m -u 1000 gateway && \
    chown -R gateway:gateway /app

# Copy installed packages from builder
COPY --from=builder /root/.local /home/gateway/.local

# Copy application code
COPY --chown=gateway:gateway . .

# Generate gRPC proto files if proto file exists
RUN if [ -f samples/backends/grpc/greeter.proto ]; then \
    python -m grpc_tools.protoc --python_out=samples/backends/grpc --grpc_python_out=samples/backends/grpc -I. samples/backends/grpc/greeter.proto || true; \
    fi

# Set environment variables
ENV PATH=/home/gateway/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Switch to non-root user
USER gateway

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD wget --no-verbose --tries=1 --spider http://localhost:8000/health || exit 1

# Run application
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

