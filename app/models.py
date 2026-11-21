"""Data models for the gateway."""
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List
from datetime import datetime
from app.time_utils import utcnow
from enum import Enum


class ProtocolType(str, Enum):
    """Supported protocol types."""
    MCP = "mcp"
    A2A = "a2a"
    AG_UI = "ag_ui"
    ACP = "acp"
    HTTP = "http"
    MQTT = "mqtt"
    WEBSOCKET = "websocket"
    GRPC = "grpc"
    GRAPHQL = "graphql"


class ProtocolSettings(BaseModel):
    """Protocol-specific settings."""
    rate_limit_per_minute: int = 60
    rate_limit_per_hour: int = 1000
    timeout: int = 30
    circuit_breaker_enabled: bool = True
    circuit_breaker_failure_threshold: int = 5
    circuit_breaker_timeout: int = 60
    max_connections: Optional[int] = None  # For WebSocket/MQTT
    keep_alive: Optional[int] = None  # For WebSocket/MQTT
    qos_level: Optional[int] = None  # For MQTT (0, 1, 2)
    compression_enabled: bool = False  # For HTTP/gRPC
    retry_enabled: bool = True
    retry_max_attempts: int = 3
    retry_backoff_ms: int = 1000
    metadata: Dict[str, Any] = Field(default_factory=dict)


class SettingsProfile(BaseModel):
    """Settings profile that can be saved and reused."""
    id: Optional[str] = None
    name: str
    description: Optional[str] = None
    protocol_settings: Dict[str, ProtocolSettings] = Field(default_factory=dict)  # Keyed by protocol
    global_settings: Dict[str, Any] = Field(default_factory=dict)
    is_default: bool = False
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    created_by: Optional[str] = None


class RouteConfig(BaseModel):
    """Route configuration."""
    path: str
    backend_url: str
    protocol: ProtocolType = ProtocolType.HTTP
    timeout: int = 30
    rate_limit: Optional[int] = None
    requires_auth: bool = True
    headers: Dict[str, str] = Field(default_factory=dict)
    rewrite_path: Optional[str] = None


class APIKey(BaseModel):
    """API Key model."""
    key: str
    id: Optional[str] = None  # database id; None for OAuth2 / break-glass principals
    name: str
    rate_limit_per_minute: int = 60
    rate_limit_per_hour: int = 1000
    allowed_paths: List[str] = Field(default_factory=list)  # Empty list means all paths
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: Optional[datetime] = None
    is_active: bool = True


class RequestLog(BaseModel):
    """Request log entry."""
    request_id: str
    method: str
    path: str
    client_ip: str
    api_key: Optional[str] = None
    status_code: int
    response_time_ms: float
    timestamp: datetime = Field(default_factory=utcnow)
    user_agent: Optional[str] = None
    error: Optional[str] = None
    # Which protocol carried the request (http, mcp, a2a, ...). Without this field the
    # middleware's setattr on the model raised and was swallowed, so the column stayed NULL.
    protocol: Optional[str] = None


class HealthStatus(BaseModel):
    """Health check response."""
    status: str
    version: str
    timestamp: datetime = Field(default_factory=utcnow)
    services: Dict[str, str] = Field(default_factory=dict)
    
    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat() if v else None
        }


class ErrorResponse(BaseModel):
    """Standard error response."""
    error: str
    message: str
    request_id: Optional[str] = None
    timestamp: datetime = Field(default_factory=utcnow)
    
    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat() if v else None
        }


class GatewayRequest(BaseModel):
    """Gateway request model."""
    method: str
    path: str
    headers: Dict[str, str] = Field(default_factory=dict)
    query_params: Dict[str, Any] = Field(default_factory=dict)
    body: Optional[Any] = None


class GatewayResponse(BaseModel):
    """Gateway response model."""
    status_code: int
    headers: Dict[str, str] = Field(default_factory=dict)
    body: Any
    response_time_ms: float
