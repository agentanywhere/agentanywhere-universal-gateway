"""Go SDK generator."""
from typing import Dict, List, Any
from app.sdk.generator import SDKGenerator
import structlog

logger = structlog.get_logger()


class GoSDKGenerator(SDKGenerator):
    """Generator for Go SDKs."""
    
    def get_language_name(self) -> str:
        """Get language name."""
        return "go"
    
    def generate(self, routes: List[Dict[str, Any]], language: str) -> Dict[str, str]:
        """Generate Go SDK."""
        endpoints = self._extract_endpoints(routes)
        
        files = {}
        
        # Generate main client file
        files["client.go"] = self._generate_client(endpoints)
        
        # Generate go.mod
        files["go.mod"] = self._generate_go_mod()
        
        # Generate README
        files["README.md"] = self._generate_readme()
        
        return files
    
    def _generate_client(self, endpoints: List[Dict[str, Any]]) -> str:
        """Generate Go client."""
        base_url = self._get_base_url()
        api_key_default = f'"{self.api_key}"' if self.api_key else '""'
        
        code = f'''// Auto-generated Go SDK for AgentAnywhere Universal Gateway.
// Generated automatically - do not edit manually.

package mcpgateway

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"
)

// Client represents the gateway client
type Client struct {{
	APIKey  string
	BaseURL string
	HTTPClient *http.Client
}}

// NewClient creates a new gateway client
func NewClient(apiKey string) *Client {{
	return &Client{{
		APIKey: apiKey,
		BaseURL: "{base_url}",
		HTTPClient: &http.Client{{
			Timeout: 30 * time.Second,
		}},
	}}
}}

// SetBaseURL sets a custom base URL
func (c *Client) SetBaseURL(url string) {{
	c.BaseURL = url
}}

// request makes an HTTP request
func (c *Client) request(method, path string, body interface{{}}) (map[string]interface{{}}, error) {{
	url := c.BaseURL + path
	
	var reqBody io.Reader
	if body != nil {{
		jsonData, err := json.Marshal(body)
		if err != nil {{
			return nil, err
		}}
		reqBody = bytes.NewBuffer(jsonData)
	}}
	
	req, err := http.NewRequest(method, url, reqBody)
	if err != nil {{
		return nil, err
	}}
	
	req.Header.Set("X-API-Key", c.APIKey)
	req.Header.Set("Content-Type", "application/json")
	
	resp, err := c.HTTPClient.Do(req)
	if err != nil {{
		return nil, err
	}}
	defer resp.Body.Close()
	
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {{
		return nil, fmt.Errorf("HTTP error: %d", resp.StatusCode)
	}}
	
	var result map[string]interface{{}}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {{
		return nil, err
	}}
	
	return result, nil
}}

'''
        
        # Generate methods
        for endpoint in endpoints:
            method_name = self._generate_method_name(endpoint["path"], endpoint["protocol"])
            # Convert to Go naming convention (PascalCase)
            method_name_go = ''.join(word.capitalize() for word in method_name.split('_'))
            path = endpoint["path"]
            protocol = endpoint["protocol"]
            
            code += f'''// {method_name_go} calls {protocol.upper()} endpoint: {path}
func (c *Client) {method_name_go}(data map[string]interface{{}}) (map[string]interface{{}}, error) {{
	method := "GET"
	if data != nil {{
		method = "POST"
	}}
	return c.request(method, "{path}", data)
}}

'''
        
        return code
    
    def _generate_go_mod(self) -> str:
        """Generate go.mod."""
        return """module github.com/mcp-gateway/client

go 1.21
"""
    
    def _generate_readme(self) -> str:
        """Generate README."""
        base_url = self._get_base_url()
        return f"""# AgentAnywhere Universal Gateway Go SDK

Auto-generated Go SDK for the AgentAnywhere Universal Gateway.

## Installation

```bash
go mod download
```

## Usage

```go
package main

import (
    "fmt"
    "log"
    mcpgateway "github.com/mcp-gateway/client"
)

func main() {{
    client := mcpgateway.NewClient("your-api-key")
    
    // Make requests
    response, err := client.McpQuery(map[string]interface{{}}{{
        "query": "example",
    }})
    if err != nil {{
        log.Fatal(err)
    }}
    
    fmt.Println(response)
}}
```

## Base URL

Default: {base_url}

You can override the base URL:

```go
client := mcpgateway.NewClient("your-api-key")
client.SetBaseURL("https://your-gateway.com")
```
"""

