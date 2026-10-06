FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Generate gRPC code from proto file
COPY greeter.proto .
RUN python -m grpc_tools.protoc --python_out=. --grpc_python_out=. -I. greeter.proto

# Copy server code
COPY grpc_server.py .

# Expose gRPC port
EXPOSE 50051

# Run the server
CMD ["python", "grpc_server.py"]


