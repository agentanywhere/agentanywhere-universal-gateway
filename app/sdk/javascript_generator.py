"""JavaScript/TypeScript SDK generator."""
from typing import Dict, List, Any
from app.sdk.generator import SDKGenerator
import structlog

logger = structlog.get_logger()


class JavaScriptSDKGenerator(SDKGenerator):
    """Generator for JavaScript/TypeScript SDKs."""
    
    def get_language_name(self) -> str:
        """Get language name."""
        return "javascript"
    
    def generate(self, routes: List[Dict[str, Any]], language: str) -> Dict[str, str]:
        """Generate JavaScript SDK."""
        endpoints = self._extract_endpoints(routes)
        
        files = {}
        
        # Generate TypeScript SDK
        files["index.ts"] = self._generate_typescript_client(endpoints)
        
        # Generate JavaScript version
        files["index.js"] = self._generate_javascript_client(endpoints)
        
        # Generate package.json
        files["package.json"] = self._generate_package_json()
        
        # Generate README
        files["README.md"] = self._generate_readme()
        
        # Generate TypeScript types
        files["types.ts"] = self._generate_types()
        
        return files
    
    def _generate_typescript_client(self, endpoints: List[Dict[str, Any]]) -> str:
        """Generate TypeScript client."""
        base_url = self._get_base_url()
        api_key_default = f'"{self.api_key}"' if self.api_key else 'undefined'
        
        code = f'''/**
 * Auto-generated TypeScript SDK for AgentAnywhere Universal Gateway.
 * Generated automatically - do not edit manually.
 */

export interface RequestOptions {{
  params?: Record<string, any>;
  headers?: Record<string, string>;
}}

export class MCPGatewayClient {{
  private apiKey: string;
  private baseUrl: string;

  constructor(apiKey: string = {api_key_default}, baseUrl: string = "{base_url}") {{
    this.apiKey = apiKey;
    this.baseUrl = baseUrl.replace(/\\/$/, '');
  }}

  private async request<T>(
    method: string,
    path: string,
    data?: any,
    options?: RequestOptions
  ): Promise<T> {{
    const url = `${{this.baseUrl}}${{path}}`;
    const headers: Record<string, string> = {{
      'X-API-Key': this.apiKey,
      'Content-Type': 'application/json',
      ...(options?.headers || {{}}),
    }};

    const response = await fetch(url, {{
      method,
      headers,
      body: data ? JSON.stringify(data) : undefined,
    }});

    if (!response.ok) {{
      throw new Error(`HTTP error! status: ${{response.status}}`);
    }}

    return response.json();
  }}

'''
        
        # Generate methods
        for endpoint in endpoints:
            method_name = self._generate_method_name(endpoint["path"], endpoint["protocol"])
            path = endpoint["path"]
            protocol = endpoint["protocol"]
            
            code += f'''  /**
   * Call {protocol.upper()} endpoint: {path}
   */
  async {method_name}<T = any>(
    data?: any,
    options?: RequestOptions
  ): Promise<T> {{
    return this.request<T>(
      data ? 'POST' : 'GET',
      '{path}',
      data,
      options
    );
  }}

'''
        
        code += "}\n"
        return code
    
    def _generate_javascript_client(self, endpoints: List[Dict[str, Any]]) -> str:
        """Generate JavaScript client (compiled from TypeScript)."""
        # For now, return a simplified JS version
        base_url = self._get_base_url()
        
        code = f'''/**
 * Auto-generated JavaScript SDK for AgentAnywhere Universal Gateway.
 */

export class MCPGatewayClient {{
  constructor(apiKey, baseUrl = "{base_url}") {{
    this.apiKey = apiKey;
    this.baseUrl = baseUrl.replace(/\\/$/, '');
  }}

  async request(method, path, data, options = {{}}) {{
    const url = `${{this.baseUrl}}${{path}}`;
    const headers = {{
      'X-API-Key': this.apiKey,
      'Content-Type': 'application/json',
      ...(options.headers || {{}}),
    }};

    const response = await fetch(url, {{
      method,
      headers,
      body: data ? JSON.stringify(data) : undefined,
    }});

    if (!response.ok) {{
      throw new Error(`HTTP error! status: ${{response.status}}`);
    }}

    return response.json();
  }}

'''
        
        for endpoint in endpoints:
            method_name = self._generate_method_name(endpoint["path"], endpoint["protocol"])
            path = endpoint["path"]
            
            code += f'''  async {method_name}(data, options) {{
    return this.request(data ? 'POST' : 'GET', '{path}', data, options);
  }}

'''
        
        code += "}\n"
        return code
    
    def _generate_package_json(self) -> str:
        """Generate package.json."""
        return '''{
  "name": "mcp-gateway-client",
  "version": "1.0.0",
  "description": "Auto-generated SDK for AgentAnywhere Universal Gateway",
  "main": "index.js",
  "types": "index.ts",
  "scripts": {
    "build": "tsc"
  },
  "keywords": ["mcp", "gateway", "sdk"],
  "author": "",
  "license": "MIT"
}
'''
    
    def _generate_readme(self) -> str:
        """Generate README."""
        return f"""# AgentAnywhere Universal Gateway JavaScript/TypeScript SDK

Auto-generated SDK for the AgentAnywhere Universal Gateway.

## Installation

```bash
npm install
```

## Usage

### TypeScript/JavaScript

```typescript
import {{ MCPGatewayClient }} from './index';

const client = new MCPGatewayClient('your-api-key');

// Make requests
const response = await client.mcpQuery({{ query: 'example' }});
console.log(response);
```

### Browser

```html
<script type="module">
  import {{ MCPGatewayClient }} from './index.js';
  
  const client = new MCPGatewayClient('your-api-key');
  const response = await client.mcpQuery({{ query: 'example' }});
</script>
```

## Base URL

Default: {self._get_base_url()}

You can override the base URL:

```typescript
const client = new MCPGatewayClient(
  'your-api-key',
  'https://your-gateway.com'
);
```
"""
    
    def _generate_types(self) -> str:
        """Generate TypeScript type definitions."""
        return """/**
 * Type definitions for AgentAnywhere Universal Gateway SDK
 */

export interface GatewayResponse<T = any> {
  data?: T;
  error?: string;
  message?: string;
}

export interface RequestOptions {
  params?: Record<string, any>;
  headers?: Record<string, string>;
}
"""


