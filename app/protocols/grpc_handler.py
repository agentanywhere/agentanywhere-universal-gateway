"""gRPC protocol handler."""
import grpc
from typing import Dict, Any, Optional
import structlog

logger = structlog.get_logger()


class GRPCHandler:
    """Handler for gRPC protocol connections."""
    
    def __init__(self, backend_url: str):
        """
        Initialize gRPC handler.
        
        Args:
            backend_url: gRPC backend URL (e.g., localhost:50051)
        """
        self.backend_url = backend_url
        self.channel: Optional[grpc.Channel] = None
        
    def create_channel(self, options: Optional[Dict[str, Any]] = None) -> grpc.Channel:
        """Create gRPC channel to backend."""
        # Get gRPC settings from configuration
        from app.config import get_settings
        settings = get_settings()
        
        default_options = [
            ('grpc.keepalive_time_ms', settings.protocol_grpc_keepalive_time_ms),
            ('grpc.keepalive_timeout_ms', settings.protocol_grpc_keepalive_timeout_ms),
            ('grpc.keepalive_permit_without_calls', True),
            ('grpc.http2.max_pings_without_data', 0),
            ('grpc.http2.min_time_between_pings_ms', settings.protocol_grpc_min_time_between_pings_ms),
            ('grpc.http2.min_ping_interval_without_data_ms', settings.protocol_grpc_min_ping_interval_without_data_ms),
        ]
        
        if options:
            default_options.extend([(f'grpc.{k}', v) for k, v in options.items()])
        
        self.channel = grpc.insecure_channel(self.backend_url, options=default_options)
        logger.info("grpc_channel_created", backend=self.backend_url)
        return self.channel
    
    def get_channel(self) -> grpc.Channel:
        """Get or create gRPC channel."""
        if not self.channel:
            return self.create_channel()
        return self.channel
    
    def call_unary(
        self,
        service_name: str,
        method_name: str,
        request_data: Dict[str, Any],
        timeout: int = 30
    ) -> Dict[str, Any]:
        """
        Make unary gRPC call using dynamic stub generation.
        
        This implementation uses grpc_reflection or generic stub calls.
        For production, you would use pre-generated stubs from .proto files.
        """
        try:
            channel = self.get_channel()
            
            logger.info(
                "grpc_call",
                service=service_name,
                method=method_name,
                backend=self.backend_url,
                request_data=request_data
            )
            
            # Try to use grpc_reflection to discover services
            try:
                import grpc_reflection.v1alpha.reflection as grpc_reflection
                from grpc_reflection.v1alpha import reflection_pb2
                
                # For now, use a simplified approach with known services
                # In production, you'd load proto files and use generated stubs
                
                # For Greeter service with SayHello method
                if service_name == "Greeter" and method_name == "SayHello":
                    # Import generated proto files
                    try:
                        import sys
                        import os
                        # Try multiple paths for proto files
                        proto_paths = [
                            os.path.join(os.path.dirname(__file__), "../../samples/backends/grpc"),
                            "/app/samples/backends/grpc",
                            "samples/backends/grpc",
                        ]
                        
                        imported = False
                        for proto_path in proto_paths:
                            if os.path.exists(proto_path):
                                sys.path.insert(0, proto_path)
                                try:
                                    import greeter_pb2
                                    import greeter_pb2_grpc
                                    imported = True
                                    break
                                except ImportError:
                                    continue
                        
                        if imported:
                            # Create stub and make call
                            stub = greeter_pb2_grpc.GreeterStub(channel)
                            request = greeter_pb2.HelloRequest(name=request_data.get("name", "World"))
                            response = stub.SayHello(request, timeout=timeout)
                            
                            logger.info("grpc_call_success", message=response.message, service=service_name, method=method_name)
                            return {
                                "status": "success",
                                "message": response.message,
                                "service": service_name,
                                "method": method_name,
                            }
                    except grpc.RpcError as e:
                        # A real gRPC call was attempted and failed. Do NOT mask
                        # this as success — surface it as a controlled backend
                        # error (previously this fell through to a fake
                        # {"status": "success"} response).
                        from app.exceptions import BackendError
                        code = e.code() if hasattr(e, "code") else None
                        logger.warning(
                            "grpc_call_failed",
                            error=str(e), code=str(code),
                            service=service_name, method=method_name,
                        )
                        raise BackendError(
                            service="gRPC",
                            message="gRPC backend call failed",
                            details={
                                "grpc_code": str(code) if code is not None else None,
                                "service": service_name,
                                "method": method_name,
                            },
                        )
                    except Exception as e:
                        logger.warning("grpc_proto_call_failed", error=str(e), using_fallback=True)
                        # Fall through to generic approach

                # Generic fallback: no generated stub was invoked, so this is NOT
                # a completed backend call. Label it honestly rather than
                # reporting a fabricated success.
                return {
                    "status": "unsupported",
                    "message": (
                        f"No gRPC stub available to bridge {service_name}.{method_name}; "
                        "request was not forwarded to the backend."
                    ),
                    "service": service_name,
                    "method": method_name,
                    "request_data": request_data,
                }

            except ImportError:
                # grpc_reflection not available.
                logger.warning("grpc_reflection_not_available", using_fallback=True)
                return {
                    "status": "unsupported",
                    "message": (
                        f"gRPC reflection is unavailable; {service_name}.{method_name} "
                        "could not be bridged to the backend."
                    ),
                    "service": service_name,
                    "method": method_name,
                    "request_data": request_data,
                }
            
        except Exception as e:
            logger.error("grpc_call_error", error=str(e), service=service_name, method=method_name)
            raise
    
    def iter_say_many_hellos(
        self,
        request_data: Dict[str, Any],
        timeout: int = 60,
    ):
        """Server-streaming Greeter.SayManyHellos (sample Tier-2 path)."""
        channel = self.get_channel()
        import sys
        import os

        proto_paths = [
            os.path.join(os.path.dirname(__file__), "../../samples/backends/grpc"),
            "/app/samples/backends/grpc",
            "samples/backends/grpc",
        ]
        for proto_path in proto_paths:
            if os.path.exists(proto_path):
                sys.path.insert(0, proto_path)
                try:
                    import greeter_pb2
                    import greeter_pb2_grpc

                    stub = greeter_pb2_grpc.GreeterStub(channel)
                    grpc_req = greeter_pb2.HelloRequest(
                        name=request_data.get("name", "World")
                    )
                    for msg in stub.SayManyHellos(grpc_req, timeout=timeout):
                        yield {
                            "status": "stream_chunk",
                            "message": msg.message,
                        }
                    return
                except ImportError:
                    continue
        raise RuntimeError(
            "SayManyHellos requires generated greeter_pb2 modules on PYTHONPATH"
        )

    def call_streaming(
        self,
        service_name: str,
        method_name: str,
        request_data: Dict[str, Any]
    ):
        """Synchronous list wrapper for streaming (testing / non-async callers)."""
        if service_name == "Greeter" and method_name == "SayManyHellos":
            return list(self.iter_say_many_hellos(request_data))
        raise ValueError(f"Streaming not implemented for {service_name}.{method_name}")
    
    def close(self):
        """Close gRPC channel."""
        if self.channel:
            self.channel.close()
            self.channel = None
            logger.info("grpc_channel_closed")

