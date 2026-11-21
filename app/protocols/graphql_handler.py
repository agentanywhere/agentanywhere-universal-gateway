"""GraphQL protocol handler."""
from typing import Dict, Any, Optional
import httpx
from app.config import get_settings
import structlog
import json

logger = structlog.get_logger()
settings = get_settings()
from app.adapters.builtin import NoCookieJar


class GraphQLHandler:
    """Handles GraphQL requests."""
    
    def __init__(self, backend_url: str):
        """Initialize GraphQL handler."""
        self.backend_url = backend_url.rstrip('/')
        # Shared by every caller: it must not keep the cookies a backend sets (see NoCookieJar).
        self.client = httpx.AsyncClient(timeout=settings.default_timeout, cookies=NoCookieJar())
        self.schema_cache: Optional[Dict[str, Any]] = None
    
    async def execute_query(
        self,
        query: str,
        variables: Optional[Dict[str, Any]] = None,
        operation_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """Execute a GraphQL query."""
        payload = {
            "query": query,
        }
        
        if variables:
            payload["variables"] = variables
        
        if operation_name:
            payload["operationName"] = operation_name
        
        try:
            # If backend_url already ends with /graphql, don't append it again
            url = self.backend_url
            if not url.endswith("/graphql"):
                url = f"{self.backend_url}/graphql"
            
            response = await self.client.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json"}
            )
            
            response.raise_for_status()
            return response.json()
        
        except httpx.HTTPStatusError as e:
            logger.error(
                "graphql_request_failed",
                status_code=e.response.status_code,
                error=str(e)
            )
            raise
        except Exception as e:
            logger.error("graphql_error", error=str(e))
            raise
    
    async def introspect_schema(self) -> Dict[str, Any]:
        """Introspect GraphQL schema."""
        if self.schema_cache:
            return self.schema_cache
        
        introspection_query = """
        query IntrospectionQuery {
            __schema {
                queryType { name }
                mutationType { name }
                subscriptionType { name }
                types {
                    ...FullType
                }
                directives {
                    name
                    description
                    locations
                    args {
                        ...InputValue
                    }
                }
            }
        }
        
        fragment FullType on __Type {
            kind
            name
            description
            fields(includeDeprecated: true) {
                name
                description
                args {
                    ...InputValue
                }
                type {
                    ...TypeRef
                }
                isDeprecated
                deprecationReason
            }
            inputFields {
                ...InputValue
            }
            interfaces {
                ...TypeRef
            }
            enumValues(includeDeprecated: true) {
                name
                description
                isDeprecated
                deprecationReason
            }
            possibleTypes {
                ...TypeRef
            }
        }
        
        fragment InputValue on __InputValue {
            name
            description
            type { ...TypeRef }
            defaultValue
        }
        
        fragment TypeRef on __Type {
            kind
            name
            ofType {
                kind
                name
                ofType {
                    kind
                    name
                    ofType {
                        kind
                        name
                        ofType {
                            kind
                            name
                            ofType {
                                kind
                                name
                                ofType {
                                    kind
                                    name
                                    ofType {
                                        kind
                                        name
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
        """
        
        try:
            result = await self.execute_query(introspection_query)
            self.schema_cache = result.get("data", {})
            return self.schema_cache
        except Exception as e:
            logger.error("graphql_introspection_failed", error=str(e))
            return {}
    
    def analyze_query_complexity(self, query: str) -> Dict[str, Any]:
        """Analyze GraphQL query complexity (simplified)."""
        # Count fields, depth, etc.
        field_count = query.count("{")
        depth = query.count("{") - query.count("}")
        
        return {
            "field_count": field_count,
            "depth": depth,
            "complexity_score": field_count * depth,
        }
    
    async def close(self):
        """Close HTTP client."""
        await self.client.aclose()

