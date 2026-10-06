"""Custom exceptions for the gateway."""
from typing import Optional, Dict, Any
from fastapi import HTTPException, status


class GatewayException(HTTPException):
    """Base exception for gateway errors."""
    
    def __init__(
        self,
        status_code: int,
        error_code: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
        request_id: Optional[str] = None,
        retry_after: Optional[int] = None
    ):
        self.error_code = error_code
        self.message = message  # Store message as attribute for handler access
        self.details = details or {}
        self.request_id = request_id
        self.retry_after = retry_after
        
        # Build detail message
        detail = {
            "error": error_code,
            "message": message,
            "request_id": request_id,
        }
        if details:
            detail["details"] = details
        
        super().__init__(status_code=status_code, detail=detail)


class AuthenticationError(GatewayException):
    """Authentication/authorization errors."""
    
    def __init__(
        self,
        message: str = "Authentication failed",
        details: Optional[Dict[str, Any]] = None,
        request_id: Optional[str] = None
    ):
        super().__init__(
            status_code=status.HTTP_401_UNAUTHORIZED,
            error_code="AUTHENTICATION_ERROR",
            message=message,
            details=details,
            request_id=request_id
        )


class AuthorizationError(GatewayException):
    """Authorization/permission errors."""
    
    def __init__(
        self,
        message: str = "Insufficient permissions",
        details: Optional[Dict[str, Any]] = None,
        request_id: Optional[str] = None
    ):
        super().__init__(
            status_code=status.HTTP_403_FORBIDDEN,
            error_code="AUTHORIZATION_ERROR",
            message=message,
            details=details,
            request_id=request_id
        )


class ValidationError(GatewayException):
    """Request validation errors."""
    
    def __init__(
        self,
        message: str = "Invalid request",
        field: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        request_id: Optional[str] = None
    ):
        if field:
            message = f"Invalid value for field '{field}': {message}"
        
        error_details = details or {}
        if field:
            error_details["field"] = field
        
        super().__init__(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_code="VALIDATION_ERROR",
            message=message,
            details=error_details,
            request_id=request_id
        )


class RateLimitError(GatewayException):
    """Rate limit exceeded errors."""
    
    def __init__(
        self,
        limit: int,
        window: str = "minute",
        retry_after: Optional[int] = None,
        request_id: Optional[str] = None
    ):
        message = f"Rate limit exceeded: {limit} requests per {window}"
        
        super().__init__(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            error_code="RATE_LIMIT_EXCEEDED",
            message=message,
            details={
                "limit": limit,
                "window": window,
                "retry_after": retry_after
            },
            request_id=request_id,
            retry_after=retry_after
        )


class BackendError(GatewayException):
    """Backend service errors."""
    
    def __init__(
        self,
        service: str,
        message: str = "Backend service unavailable",
        details: Optional[Dict[str, Any]] = None,
        request_id: Optional[str] = None
    ):
        error_details = details or {}
        error_details["service"] = service
        
        super().__init__(
            status_code=status.HTTP_502_BAD_GATEWAY,
            error_code="BACKEND_ERROR",
            message=f"{service}: {message}",
            details=error_details,
            request_id=request_id
        )


class ServiceUnavailableError(GatewayException):
    """Service unavailable errors."""
    
    def __init__(
        self,
        service: str,
        message: str = "Service not configured",
        details: Optional[Dict[str, Any]] = None,
        request_id: Optional[str] = None
    ):
        error_details = details or {}
        error_details["service"] = service
        
        super().__init__(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            error_code="SERVICE_UNAVAILABLE",
            message=f"{service}: {message}",
            details=error_details,
            request_id=request_id
        )


class NotFoundError(GatewayException):
    """Resource not found errors."""
    
    def __init__(
        self,
        resource: str,
        resource_id: Optional[str] = None,
        request_id: Optional[str] = None
    ):
        if resource_id:
            message = f"{resource} '{resource_id}' not found"
        else:
            message = f"{resource} not found"
        
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            error_code="NOT_FOUND",
            message=message,
            details={"resource": resource, "resource_id": resource_id},
            request_id=request_id
        )


class InternalServerError(GatewayException):
    """Internal server errors (should not expose details to users)."""
    
    def __init__(
        self,
        message: str = "An internal error occurred",
        request_id: Optional[str] = None,
        log_error: Optional[str] = None
    ):
        # Don't expose internal error details to users
        super().__init__(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_code="INTERNAL_ERROR",
            message=message,
            details={"request_id": request_id} if request_id else {},
            request_id=request_id
        )
        # Store internal error for logging
        self.log_error = log_error

