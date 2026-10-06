"""Python SDK generator."""
from typing import Dict, List, Any
from app.sdk.generator import SDKGenerator
import structlog

logger = structlog.get_logger()


class PythonSDKGenerator(SDKGenerator):
    """Generator for Python SDKs."""
    
    def get_language_name(self) -> str:
        """Get language name."""
        return "python"
    
    def generate(self, routes: List[Dict[str, Any]], language: str) -> Dict[str, str]:
        """Generate Python SDK."""
        endpoints = self._extract_endpoints(routes)
        
        files = {}
        
        # Generate main SDK file
        files["mcp_gateway_client.py"] = self._generate_client_class(endpoints)
        
        # Generate requirements.txt
        files["requirements.txt"] = self._generate_requirements()
        
        # Generate README
        files["README.md"] = self._generate_readme()
        
        # Generate __init__.py
        files["__init__.py"] = self._generate_init()
        
        return files
    
    def _generate_client_class(self, endpoints: List[Dict[str, Any]]) -> str:
        """Generate Python client class."""
        base_url = self._get_base_url()
        api_key_param = f'api_key: str = "{self.api_key}"' if self.api_key else "api_key: str"
        
        code = f'''"""
Auto-generated Python SDK for AgentAnywhere Universal Gateway.
Generated automatically - do not edit manually.
"""

import httpx
from typing import Optional, Dict, Any, List
from datetime import datetime


class MCPGatewayClient:
    """Client for AgentAnywhere Universal Gateway API."""
    
    def __init__(self, {api_key_param}, base_url: str = "{base_url}"):
        """
        Initialize gateway client.
        
        Args:
            api_key: Your API key
            base_url: Gateway base URL (default: {base_url})
        """
        self.api_key = api_key
        self.base_url = base_url.rstrip('/')
        self.client = httpx.Client(timeout=30.0)
    
    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        """Make HTTP request."""
        url = f"{{self.base_url}}{{path}}"
        request_headers = {{
            "X-API-Key": self.api_key,
            "Content-Type": "application/json",
        }}
        if headers:
            request_headers.update(headers)
        
        response = self.client.request(
            method=method,
            url=url,
            params=params,
            json=json,
            headers=request_headers
        )
        response.raise_for_status()
        return response.json()
    
'''
        
        # Generate methods for each endpoint
        for endpoint in endpoints:
            method_name = self._generate_method_name(endpoint["path"], endpoint["protocol"])
            path = endpoint["path"]
            protocol = endpoint["protocol"]
            
            code += f'''    def {method_name}(
        self,
        data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Call {protocol.upper()} endpoint: {path}
        
        Args:
            data: Request body data
            params: Query parameters
            
        Returns:
            Response data
        """
        return self._request(
            method="POST" if data else "GET",
            path="{path}",
            params=params,
            json=data
        )
    
'''
        
        # Add async client
        code += '''
    async def _async_request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        """Make async HTTP request."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            url = f"{self.base_url}{path}"
            request_headers = {
                "X-API-Key": self.api_key,
                "Content-Type": "application/json",
            }
            if headers:
                request_headers.update(headers)
            
            response = await client.request(
                method=method,
                url=url,
                params=params,
                json=json,
                headers=request_headers
            )
            response.raise_for_status()
            return response.json()


class AsyncMCPGatewayClient:
    """Async client for AgentAnywhere Universal Gateway API."""
    
    def __init__(self, api_key: str, base_url: str = "{base_url}"):
        """
        Initialize async gateway client.
        
        Args:
            api_key: Your API key
            base_url: Gateway base URL (default: {base_url})
        """
        self.api_key = api_key
        self.base_url = base_url.rstrip('/')
    
'''
        
        # Generate async methods
        for endpoint in endpoints:
            method_name = self._generate_method_name(endpoint["path"], endpoint["protocol"])
            path = endpoint["path"]
            protocol = endpoint["protocol"]
            
            code += f'''    async def {method_name}(
        self,
        data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Call {protocol.upper()} endpoint: {path} (async)
        
        Args:
            data: Request body data
            params: Query parameters
            
        Returns:
            Response data
        """
        return await self._async_request(
            method="POST" if data else "GET",
            path="{path}",
            params=params,
            json=data
        )
    
'''
        
        return code
    
    def _generate_requirements(self) -> str:
        """Generate requirements.txt."""
        return """httpx>=0.25.0
"""
    
    def _generate_readme(self) -> str:
        """Generate README."""
        return f"""# AgentAnywhere Universal Gateway Python SDK

Auto-generated Python SDK for the AgentAnywhere Universal Gateway.

## Installation

```bash
pip install -r requirements.txt
```

## Usage

### Synchronous Client

```python
from mcp_gateway_client import MCPGatewayClient

client = MCPGatewayClient(api_key="your-api-key")

# Make requests
response = client.mcp_query(data={{"query": "example"}})
print(response)
```

### Async Client

```python
from mcp_gateway_client import AsyncMCPGatewayClient
import asyncio

async def main():
    client = AsyncMCPGatewayClient(api_key="your-api-key")
    response = await client.mcp_query(data={{"query": "example"}})
    print(response)

asyncio.run(main())
```

## Base URL

Default: {self._get_base_url()}

You can override the base URL when initializing the client:

```python
client = MCPGatewayClient(
    api_key="your-api-key",
    base_url="https://your-gateway.com"
)
```
"""
    
    def _generate_init(self) -> str:
        """Generate __init__.py."""
        return """from .mcp_gateway_client import MCPGatewayClient, AsyncMCPGatewayClient

__all__ = ["MCPGatewayClient", "AsyncMCPGatewayClient"]
"""


