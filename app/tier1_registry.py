"""In-memory registry of OpenAPI registrations + Agent Card / MCP manifest synthesis."""
from __future__ import annotations

import copy
from typing import Any, Dict, List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db_models import OpenAPIRegistrationModel
from app.tier1_openapi import (
    registration_to_a2a_skills,
    registration_to_mcp_tools,
    validate_openapi_document,
)


class Tier1Registry:
    _rows: List[Dict[str, Any]] = []

    @classmethod
    async def reload(cls, db: AsyncSession) -> None:
        result = await db.execute(
            select(OpenAPIRegistrationModel).where(OpenAPIRegistrationModel.is_active.is_(True))
        )
        rows = result.scalars().all()
        next_rows: List[Dict[str, Any]] = []
        for r in rows:
            next_rows.append(
                {
                    "id": r.id,
                    "name": r.name,
                    "description": r.description,
                    "openapi_document": copy.deepcopy(r.openapi_document),
                    "upstream_base_url": r.upstream_base_url,
                    "path_prefix": r.path_prefix or "",
                    "expose_mcp_tools": bool(r.expose_mcp_tools),
                    "expose_a2a_skills": bool(r.expose_a2a_skills),
                }
            )
        cls._rows = next_rows

    @classmethod
    def list_registrations(cls) -> List[Dict[str, Any]]:
        """Return metadata for list endpoints (omit OpenAPI document)."""
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "description": r["description"],
                "upstream_base_url": r["upstream_base_url"],
                "path_prefix": r.get("path_prefix", ""),
                "expose_mcp_tools": r.get("expose_mcp_tools", True),
                "expose_a2a_skills": r.get("expose_a2a_skills", True),
            }
            for r in cls._rows
        ]

    @classmethod
    def get_registration(cls, reg_id: str) -> Dict[str, Any] | None:
        for r in cls._rows:
            if r["id"] == reg_id:
                return copy.deepcopy(r)
        return None

    @classmethod
    def aggregated_mcp_tools(cls) -> List[Dict[str, Any]]:
        tools: List[Dict[str, Any]] = []
        for r in cls._rows:
            if not r.get("expose_mcp_tools", True):
                continue
            try:
                validate_openapi_document(r["openapi_document"])
                tools.extend(registration_to_mcp_tools(r))
            except ValueError:
                continue
        return tools

    @classmethod
    def tool_input_schema_index(cls) -> Dict[str, Dict[str, Any]]:
        idx: Dict[str, Dict[str, Any]] = {}
        for t in cls.aggregated_mcp_tools():
            name = t.get("name")
            sch = t.get("inputSchema")
            if isinstance(name, str) and isinstance(sch, dict):
                idx[name] = sch
        return idx

    @classmethod
    def build_agent_card(cls) -> Dict[str, Any]:
        settings = get_settings()
        gateway_url = settings.gateway_url_base or f"http://{settings.host}:{settings.port}"
        gateway_url = gateway_url.rstrip("/")
        interfaces = [
            {
                "url": f"{gateway_url}/a2a",
                "protocolBinding": "HTTP+JSON",
                "protocolVersion": "1.0",
            },
        ]
        if settings.ag_ui_backend_url:
            interfaces.append(
                {
                    "url": f"{gateway_url}/agui",
                    "protocolBinding": "HTTP+JSON",
                    "protocolVersion": "1.0",
                }
            )
        if settings.acp_backend_url:
            interfaces.append(
                {
                    "url": f"{gateway_url}/acp",
                    "protocolBinding": "HTTP+JSON",
                    "protocolVersion": "0.2",
                }
            )
        skills: List[Dict[str, Any]] = []
        for r in cls._rows:
            if not r.get("expose_a2a_skills", True):
                continue
            try:
                validate_openapi_document(r["openapi_document"])
                skills.extend(registration_to_a2a_skills(r))
            except ValueError:
                continue
        skills.insert(
            0,
            {
                "id": "gateway.proxy",
                "name": "Gateway proxy",
                "description": (
                    "Forward A2A and MCP traffic to configured backends with gateway "
                    "authentication, rate limits, and observability."
                ),
                "tags": ["gateway", "proxy"],
                "examples": ["Delegate a task through the secured gateway"],
                "inputModes": ["application/json", "text/plain"],
                "outputModes": ["application/json", "text/plain"],
            },
        )
        return {
            "name": settings.app_name,
            "description": (
                f"{settings.app_name} — multi-protocol AI gateway (MCP, A2A, HTTP). "
                "OpenAPI-registered REST surfaces are advertised as skills for discovery."
            ),
            "version": settings.app_version,
            "protocolVersion": "1.0",
            "provider": {
                "organization": settings.app_name,
                "url": gateway_url,
            },
            "documentationUrl": f"{gateway_url}/docs",
            "capabilities": {
                "streaming": True,
                "pushNotifications": False,
                "extensions": [],
                "extendedAgentCard": False,
            },
            "defaultInputModes": ["application/json", "text/plain"],
            "defaultOutputModes": ["application/json", "text/plain"],
            "supportedInterfaces": interfaces,
            "skills": skills[:2000],
            "securitySchemes": {
                "gatewayApiKey": {
                    "apiKeySecurityScheme": {
                        "location": "header",
                        "name": settings.api_key_header,
                        "description": (
                            "Gateway-issued API key; OAuth2 access tokens may be sent "
                            "via the Authorization header."
                        ),
                    }
                }
            },
            "securityRequirements": [{"gatewayApiKey": []}],
        }


tier1_registry = Tier1Registry
