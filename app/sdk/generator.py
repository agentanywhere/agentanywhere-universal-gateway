"""Base SDK generator."""
from abc import ABC, abstractmethod
from typing import Dict, List, Any, Optional
from app.models import ProtocolType
import structlog

logger = structlog.get_logger()


class SDKGenerator(ABC):
    """Base class for SDK generators."""
    
    def __init__(self, gateway_url: str, api_key: Optional[str] = None):
        """
        Initialize SDK generator.
        
        Args:
            gateway_url: Base URL of the gateway
            api_key: Optional API key for authenticated endpoints
        """
        self.gateway_url = gateway_url.rstrip('/')
        self.api_key = api_key
        self.supported_protocols = [
            ProtocolType.MCP,
            ProtocolType.A2A,
            ProtocolType.AG_UI,
            ProtocolType.ACP,
            ProtocolType.HTTP,
        ]
    
    @abstractmethod
    def generate(self, routes: List[Dict[str, Any]], language: str) -> Dict[str, str]:
        """
        Generate SDK files.
        
        Args:
            routes: List of route configurations
            language: Target language
            
        Returns:
            Dictionary mapping file paths to file contents
        """
        pass
    
    @abstractmethod
    def get_language_name(self) -> str:
        """Get the language name."""
        pass
    
    def _extract_endpoints(self, routes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Extract endpoint information from routes."""
        endpoints = []
        
        for route in routes:
            protocol = route.get("protocol", "http")
            path = route.get("path", "")
            backend_url = route.get("backend_url", "")
            
            # Generate endpoint info
            endpoint = {
                "path": path,
                "protocol": protocol,
                "backend_url": backend_url,
                "requires_auth": route.get("requires_auth", True),
                "timeout": route.get("timeout", 30),
            }
            
            endpoints.append(endpoint)
        
        return endpoints
    
    def _generate_method_name(self, path: str, protocol: str) -> str:
        """Generate method name from path."""
        # Remove leading slash and protocol prefix
        clean_path = path.lstrip('/').replace(f"{protocol}/", "").replace("api/", "")
        
        # Replace slashes and special chars with underscores
        method_name = clean_path.replace('/', '_').replace('-', '_').replace('*', 'all')
        
        # Remove trailing underscores
        method_name = method_name.rstrip('_')
        
        # Convert to camelCase or snake_case based on language
        if not method_name:
            method_name = f"{protocol}_request"
        
        return method_name
    
    def _get_base_url(self) -> str:
        """Get base URL for SDK."""
        return self.gateway_url


class SDKService:
    """Service for managing SDK generation."""
    
    def __init__(self, gateway_url: str):
        """Initialize SDK service."""
        self.gateway_url = gateway_url
        self.generators: Dict[str, SDKGenerator] = {}
    
    def register_generator(self, language: str, generator: SDKGenerator):
        """Register an SDK generator."""
        self.generators[language] = generator
        logger.info("sdk_generator_registered", language=language)
    
    def generate_sdk(
        self,
        language: str,
        routes: List[Dict[str, Any]],
        api_key: Optional[str] = None
    ) -> Dict[str, str]:
        """
        Generate SDK for a language.
        
        Args:
            language: Target language (python, javascript, go)
            routes: List of route configurations
            api_key: Optional API key
            
        Returns:
            Dictionary mapping file paths to file contents
        """
        if language not in self.generators:
            raise ValueError(f"Unsupported language: {language}")
        
        generator = self.generators[language]
        return generator.generate(routes, language)
    
    def get_supported_languages(self) -> List[str]:
        """Get list of supported languages."""
        return list(self.generators.keys())


