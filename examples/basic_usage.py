"""
Basic Usage Example - AgentAnywhere Universal Gateway

This example demonstrates how to use the gateway with different protocols.

Usage:
    # Set API key as environment variable
    export API_KEY=your-api-key-here
    python examples/basic_usage.py
    
    # Or edit API_KEY in this file
"""
import os
import requests
import json
from typing import Dict, Any

# Gateway configuration
GATEWAY_URL = "http://localhost:8000"
API_KEY = os.getenv("API_KEY", "your-api-key-here")  # Set via environment or replace here


def make_request(
    endpoint: str,
    method: str = "GET",
    data: Dict[str, Any] = None,
    headers: Dict[str, str] = None
) -> Dict[str, Any]:
    """Make a request through the gateway."""
    url = f"{GATEWAY_URL}{endpoint}"
    default_headers = {
        "X-API-Key": API_KEY,
        "Content-Type": "application/json"
    }
    if headers:
        default_headers.update(headers)
    
    response = requests.request(
        method=method,
        url=url,
        json=data,
        headers=default_headers
    )
    response.raise_for_status()
    return response.json()


def example_mcp_request():
    """Example: Make an MCP (Model Context Protocol) request."""
    print("=== MCP Request Example ===")
    
    data = {
        "query": "What is the weather today?",
        "context": {"user_id": "user123"}
    }
    
    try:
        result = make_request("/mcp/query", method="POST", data=data)
        print(f"Response: {json.dumps(result, indent=2)}")
    except requests.exceptions.RequestException as e:
        print(f"Error: {e}")


def example_a2a_request():
    """Example: Make an A2A (Agent-to-Agent) request."""
    print("\n=== A2A Request Example ===")
    
    data = {
        "message": "Hello from agent A",
        "target_agent": "agent_b",
        "metadata": {"priority": "high"}
    }
    
    try:
        result = make_request("/a2a/message", method="POST", data=data)
        print(f"Response: {json.dumps(result, indent=2)}")
    except requests.exceptions.RequestException as e:
        print(f"Error: {e}")


def example_mqtt_publish():
    """Example: Publish to MQTT topic."""
    print("\n=== MQTT Publish Example ===")
    
    data = {
        "topic": "sensors/temperature",
        "payload": {
            "value": 25.5,
            "unit": "celsius",
            "timestamp": "2025-12-03T10:00:00Z"
        },
        "qos": 1
    }
    
    try:
        result = make_request("/mqtt/publish", method="POST", data=data)
        print(f"Published: {json.dumps(result, indent=2)}")
    except requests.exceptions.RequestException as e:
        print(f"Error: {e}")


def example_http_request():
    """Example: Make a standard HTTP/REST request."""
    print("\n=== HTTP/REST Request Example ===")
    
    data = {
        "action": "get_user",
        "user_id": "user123"
    }
    
    try:
        result = make_request("/api/users", method="POST", data=data)
        print(f"Response: {json.dumps(result, indent=2)}")
    except requests.exceptions.RequestException as e:
        print(f"Error: {e}")


def example_grpc_request():
    """Example: Make a gRPC unary call through the gateway.

    NOT WORKING: the gateway's JSON-to-gRPC bridge does not forward calls to a
    backend. With the stock requirements it answers HTTP 200 with
    "status": "unsupported". This shows the intended interface; see
    docs/PROTOCOLS.md.
    """
    print("\n=== gRPC Request Example ===")

    data = {
        "service": "Greeter",
        "method": "SayHello",
        "data": {"name": "world"},
    }

    try:
        result = make_request("/grpc/call", method="POST", data=data)
        print(f"Response: {json.dumps(result, indent=2)}")
    except requests.exceptions.RequestException as e:
        print(f"Error: {e}")


def example_graphql_request():
    """Example: Make a GraphQL query through the gateway."""
    print("\n=== GraphQL Request Example ===")

    data = {"query": "{ hello }"}

    try:
        result = make_request("/graphql", method="POST", data=data)
        print(f"Response: {json.dumps(result, indent=2)}")
    except requests.exceptions.RequestException as e:
        print(f"Error: {e}")


def example_get_health():
    """Example: Check gateway health (no auth required)."""
    print("\n=== Health Check Example ===")
    
    try:
        response = requests.get(f"{GATEWAY_URL}/health")
        result = response.json()
        print(f"Status: {result.get('status')}")
        print(f"Version: {result.get('version')}")
    except requests.exceptions.RequestException as e:
        print(f"Error: {e}")


def example_with_error_handling():
    """Example: Proper error handling."""
    print("\n=== Error Handling Example ===")
    
    try:
        # This will fail without proper API key
        result = make_request("/mcp/query", method="POST", data={})
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 401:
            print("Authentication failed - check your API key")
        elif e.response.status_code == 429:
            print("Rate limit exceeded - slow down your requests")
        else:
            print(f"HTTP Error: {e.response.status_code}")
    except requests.exceptions.RequestException as e:
        print(f"Request failed: {e}")


if __name__ == "__main__":
    print("AgentAnywhere Universal Gateway - Basic Usage Examples\n")
    print("=" * 50)
    
    # Run examples
    example_get_health()
    # Uncomment to test with actual backend:
    # example_mcp_request()
    # example_a2a_request()
    # example_mqtt_publish()
    # example_http_request()
    # example_grpc_request()
    # example_graphql_request()
    example_with_error_handling()
    
    print("\n" + "=" * 50)
    print("Examples completed!")

