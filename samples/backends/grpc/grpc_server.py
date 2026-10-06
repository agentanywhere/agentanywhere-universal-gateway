"""
Sample gRPC Backend Server

This is a simple gRPC server that demonstrates gRPC integration.
"""
import grpc
from concurrent import futures
import greeter_pb2
import greeter_pb2_grpc
import structlog
import time

logger = structlog.get_logger()


class GreeterServicer(greeter_pb2_grpc.GreeterServicer):
    """Implementation of the Greeter service."""

    def SayHello(self, request, context):
        """SayHello implementation."""
        logger.info("grpc_say_hello", name=request.name)
        return greeter_pb2.HelloReply(
            message=f"Hello, {request.name}! This is a gRPC response from the backend."
        )

    def SayManyHellos(self, request, context):
        """Server-streaming greeting (Tier-2 sample)."""
        name = request.name or "World"
        logger.info("grpc_say_many_hellos", name=name)
        for i in range(1, 4):
            yield greeter_pb2.HelloReply(
                message=f"[stream {i}/3] Hello, {name}! from streaming Greeter."
            )


def serve():
    """Start the gRPC server."""
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    greeter_pb2_grpc.add_GreeterServicer_to_server(GreeterServicer(), server)
    
    # Listen on port 50051
    server.add_insecure_port('[::]:50051')
    server.start()
    
    logger.info("grpc_server_started", port=50051)
    
    try:
        while True:
            time.sleep(86400)  # One day
    except KeyboardInterrupt:
        server.stop(0)


if __name__ == "__main__":
    serve()
