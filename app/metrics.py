"""Metrics collection for Prometheus."""
from prometheus_client import Counter, Histogram, Gauge
from app.config import get_settings

settings = get_settings()

# Request metrics
request_count = Counter(
    "gateway_requests_total",
    "Total number of requests",
    ["method", "path", "status_code", "protocol"]
)

request_duration = Histogram(
    "gateway_request_duration_seconds",
    "Request duration in seconds",
    ["method", "path", "protocol"],
    buckets=(0.01, 0.05, 0.1, 0.5, 1.0, 2.0, 5.0, 10.0)
)

active_connections = Gauge(
    "gateway_active_connections",
    "Number of active connections"
)

# Error metrics
error_count = Counter(
    "gateway_errors_total",
    "Total number of errors",
    ["error_type", "protocol"]
)

# Rate limit metrics
rate_limit_hits = Counter(
    "gateway_rate_limit_hits_total",
    "Total number of rate limit hits",
    ["client_id"]
)

# Data-plane requests authenticated with SECRET_KEY (break-glass)
secret_key_api_key_uses = Counter(
    "gateway_secret_key_api_key_uses_total",
    "Proxy requests authenticated with SECRET_KEY instead of an API key",
)

# Circuit breaker metrics
circuit_breaker_state = Gauge(
    "gateway_circuit_breaker_state",
    "Circuit breaker state (0=closed, 1=open, 2=half-open)",
    ["backend_url"]
)


