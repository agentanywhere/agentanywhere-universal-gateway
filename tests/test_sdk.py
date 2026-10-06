"""Tests for SDK generation."""
import pytest
from unittest.mock import Mock, patch
from app.sdk.generator import SDKGenerator, SDKService
from app.sdk.python_generator import PythonSDKGenerator
from app.sdk.javascript_generator import JavaScriptSDKGenerator
from app.sdk.go_generator import GoSDKGenerator
from app.models import ProtocolType


class TestSDKGenerator:
    """Tests for base SDK generator."""
    
    def test_init(self):
        """Test SDK generator initialization."""
        generator = PythonSDKGenerator("http://localhost:8000", "test-key")
        assert generator.gateway_url == "http://localhost:8000"
        assert generator.api_key == "test-key"
    
    def test_extract_endpoints(self):
        """Test endpoint extraction."""
        generator = PythonSDKGenerator("http://localhost:8000")
        routes = [
            {
                "path": "/mcp/query",
                "protocol": "mcp",
                "backend_url": "http://backend:8001",
                "requires_auth": True,
                "timeout": 30
            },
            {
                "path": "/a2a/message",
                "protocol": "a2a",
                "backend_url": "http://backend:8002",
                "requires_auth": True,
                "timeout": 30
            }
        ]
        
        endpoints = generator._extract_endpoints(routes)
        assert len(endpoints) == 2
        assert endpoints[0]["path"] == "/mcp/query"
        assert endpoints[0]["protocol"] == "mcp"
    
    def test_generate_method_name(self):
        """Test method name generation."""
        generator = PythonSDKGenerator("http://localhost:8000")
        
        assert generator._generate_method_name("/mcp/query", "mcp") == "query"
        assert generator._generate_method_name("/a2a/message/send", "a2a") == "message_send"
        assert generator._generate_method_name("/api/users", "http") == "users"
        assert generator._generate_method_name("/", "http") == "http_request"
    
    def test_get_base_url(self):
        """Test base URL generation."""
        generator = PythonSDKGenerator("http://localhost:8000")
        assert generator._get_base_url() == "http://localhost:8000"


class TestPythonSDKGenerator:
    """Tests for Python SDK generator."""
    
    def test_init(self):
        """Test Python generator initialization."""
        generator = PythonSDKGenerator("http://localhost:8000")
        assert generator.get_language_name() == "python"
    
    def test_generate(self):
        """Test Python SDK generation."""
        generator = PythonSDKGenerator("http://localhost:8000", "test-key")
        routes = [
            {
                "path": "/mcp/query",
                "protocol": "mcp",
                "backend_url": "http://backend:8001",
                "requires_auth": True,
                "timeout": 30
            }
        ]
        
        files = generator.generate(routes, "python")
        
        assert "mcp_gateway_client.py" in files
        assert "requirements.txt" in files
        assert "README.md" in files
        assert "__init__.py" in files
        
        # Check client file contains expected content
        client_code = files["mcp_gateway_client.py"]
        assert "class MCPGatewayClient" in client_code
        assert "class AsyncMCPGatewayClient" in client_code
        assert "test-key" in client_code or "api_key" in client_code
        assert "http://localhost:8000" in client_code
    
    def test_generate_with_multiple_endpoints(self):
        """Test generation with multiple endpoints."""
        generator = PythonSDKGenerator("http://localhost:8000")
        routes = [
            {"path": "/mcp/query", "protocol": "mcp", "backend_url": "http://backend:8001"},
            {"path": "/a2a/message", "protocol": "a2a", "backend_url": "http://backend:8002"},
            {"path": "/api/users", "protocol": "http", "backend_url": "http://backend:8003"},
        ]
        
        files = generator.generate(routes, "python")
        client_code = files["mcp_gateway_client.py"]
        
        # Check all endpoints are included
        assert "def query" in client_code or "def mcp_query" in client_code
        assert "def message" in client_code or "def a2a_message" in client_code
        assert "def users" in client_code or "def api_users" in client_code
    
    def test_generate_requirements(self):
        """Test requirements.txt generation."""
        generator = PythonSDKGenerator("http://localhost:8000")
        requirements = generator._generate_requirements()
        assert "httpx" in requirements
    
    def test_generate_readme(self):
        """Test README generation."""
        generator = PythonSDKGenerator("http://localhost:8000")
        readme = generator._generate_readme()
        assert "AgentAnywhere Universal Gateway Python SDK" in readme
        assert "MCPGatewayClient" in readme
        assert "AsyncMCPGatewayClient" in readme


class TestJavaScriptSDKGenerator:
    """Tests for JavaScript SDK generator."""
    
    def test_init(self):
        """Test JavaScript generator initialization."""
        generator = JavaScriptSDKGenerator("http://localhost:8000")
        assert generator.get_language_name() == "javascript"
    
    def test_generate(self):
        """Test JavaScript SDK generation."""
        generator = JavaScriptSDKGenerator("http://localhost:8000")
        routes = [
            {
                "path": "/mcp/query",
                "protocol": "mcp",
                "backend_url": "http://backend:8001",
                "requires_auth": True,
            }
        ]
        
        files = generator.generate(routes, "javascript")
        
        assert "index.ts" in files
        assert "index.js" in files
        assert "package.json" in files
        assert "README.md" in files
        assert "types.ts" in files
        
        # Check TypeScript file
        ts_code = files["index.ts"]
        assert "class MCPGatewayClient" in ts_code
        assert "http://localhost:8000" in ts_code
    
    def test_generate_package_json(self):
        """Test package.json generation."""
        generator = JavaScriptSDKGenerator("http://localhost:8000")
        package_json = generator._generate_package_json()
        assert '"name": "mcp-gateway-client"' in package_json
        assert '"version": "1.0.0"' in package_json


class TestGoSDKGenerator:
    """Tests for Go SDK generator."""
    
    def test_init(self):
        """Test Go generator initialization."""
        generator = GoSDKGenerator("http://localhost:8000")
        assert generator.get_language_name() == "go"
    
    def test_generate(self):
        """Test Go SDK generation."""
        generator = GoSDKGenerator("http://localhost:8000")
        routes = [
            {
                "path": "/mcp/query",
                "protocol": "mcp",
                "backend_url": "http://backend:8001",
                "requires_auth": True,
            }
        ]
        
        files = generator.generate(routes, "go")
        
        assert "client.go" in files
        assert "go.mod" in files
        assert "README.md" in files
        
        # Check Go code
        go_code = files["client.go"]
        assert "type Client struct" in go_code
        assert "func NewClient" in go_code
        assert "http://localhost:8000" in go_code
    
    def test_generate_go_mod(self):
        """Test go.mod generation."""
        generator = GoSDKGenerator("http://localhost:8000")
        go_mod = generator._generate_go_mod()
        assert "module github.com/mcp-gateway/client" in go_mod
        assert "go 1.21" in go_mod


class TestSDKService:
    """Tests for SDK service."""
    
    def test_init(self):
        """Test SDK service initialization."""
        service = SDKService("http://localhost:8000")
        assert service.gateway_url == "http://localhost:8000"
        assert service.generators == {}
    
    def test_register_generator(self):
        """Test generator registration."""
        service = SDKService("http://localhost:8000")
        generator = PythonSDKGenerator("http://localhost:8000")
        
        service.register_generator("python", generator)
        assert "python" in service.generators
        assert service.generators["python"] == generator
    
    def test_get_supported_languages(self):
        """Test getting supported languages."""
        service = SDKService("http://localhost:8000")
        service.register_generator("python", PythonSDKGenerator("http://localhost:8000"))
        service.register_generator("javascript", JavaScriptSDKGenerator("http://localhost:8000"))
        
        languages = service.get_supported_languages()
        assert "python" in languages
        assert "javascript" in languages
        assert len(languages) == 2
    
    def test_generate_sdk(self):
        """Test SDK generation."""
        service = SDKService("http://localhost:8000")
        service.register_generator("python", PythonSDKGenerator("http://localhost:8000"))
        
        routes = [
            {
                "path": "/mcp/query",
                "protocol": "mcp",
                "backend_url": "http://backend:8001",
                "requires_auth": True,
            }
        ]
        
        files = service.generate_sdk("python", routes)
        assert "mcp_gateway_client.py" in files
    
    def test_generate_sdk_unsupported_language(self):
        """Test SDK generation with unsupported language."""
        service = SDKService("http://localhost:8000")
        
        with pytest.raises(ValueError, match="Unsupported language"):
            service.generate_sdk("rust", [])


