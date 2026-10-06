"""Route repository for database operations."""
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.db_models import RouteModel
from app.models import RouteConfig, ProtocolType
from typing import Optional, List
import uuid


class RouteRepository:
    """Repository for route operations."""
    
    @staticmethod
    async def create(
        db: AsyncSession,
        route_config: RouteConfig,
    ) -> RouteModel:
        """Create a new route."""
        route_id = str(uuid.uuid4())
        db_route = RouteModel(
            id=route_id,
            path=route_config.path,
            backend_url=route_config.backend_url,
            protocol=route_config.protocol.value,
            timeout=route_config.timeout,
            rate_limit=route_config.rate_limit,
            requires_auth=route_config.requires_auth,
            headers=route_config.headers,
            rewrite_path=route_config.rewrite_path,
            is_active=True,
        )
        
        db.add(db_route)
        await db.commit()
        await db.refresh(db_route)
        return db_route
    
    @staticmethod
    async def get_by_id(db: AsyncSession, route_id: str) -> Optional[RouteModel]:
        """Get route by ID."""
        result = await db.execute(
            select(RouteModel).where(RouteModel.id == route_id)
        )
        return result.scalar_one_or_none()
    
    @staticmethod
    async def get_by_path(db: AsyncSession, path: str) -> Optional[RouteModel]:
        """Get route by path (exact match)."""
        result = await db.execute(
            select(RouteModel).where(RouteModel.path == path, RouteModel.is_active == True)
        )
        return result.scalar_one_or_none()
    
    @staticmethod
    async def find_matching_route(db: AsyncSession, request_path: str) -> Optional[RouteModel]:
        """Find route that matches request path (supports wildcards)."""
        # Get all active routes
        result = await db.execute(
            select(RouteModel).where(RouteModel.is_active == True)
        )
        routes = list(result.scalars().all())
        
        # Find matching route
        for route in routes:
            route_path = route.path
            # Support wildcard matching
            if route_path.endswith('*'):
                prefix = route_path[:-1]
                if request_path.startswith(prefix):
                    return route
            elif route_path == request_path:
                return route
        
        return None
    
    @staticmethod
    async def list_all(db: AsyncSession, active_only: bool = False) -> List[RouteModel]:
        """List all routes."""
        query = select(RouteModel)
        if active_only:
            query = query.where(RouteModel.is_active == True)
        result = await db.execute(query)
        return list(result.scalars().all())
    
    @staticmethod
    async def update(db: AsyncSession, route_id: str, route_config: RouteConfig) -> Optional[RouteModel]:
        """Update route."""
        route = await RouteRepository.get_by_id(db, route_id)
        if route:
            route.path = route_config.path
            route.backend_url = route_config.backend_url
            route.protocol = route_config.protocol.value
            route.timeout = route_config.timeout
            route.rate_limit = route_config.rate_limit
            route.requires_auth = route_config.requires_auth
            route.headers = route_config.headers
            route.rewrite_path = route_config.rewrite_path
            await db.commit()
            await db.refresh(route)
        return route
    
    @staticmethod
    async def delete(db: AsyncSession, route_id: str) -> bool:
        """Delete route."""
        route = await RouteRepository.get_by_id(db, route_id)
        if route:
            await db.delete(route)
            await db.commit()
            return True
        return False
    
    @staticmethod
    def to_pydantic(db_route: RouteModel) -> RouteConfig:
        """Convert database model to Pydantic model."""
        return RouteConfig(
            path=db_route.path,
            backend_url=db_route.backend_url,
            protocol=ProtocolType(db_route.protocol),
            timeout=db_route.timeout,
            rate_limit=db_route.rate_limit,
            requires_auth=db_route.requires_auth,
            headers=db_route.headers or {},
            rewrite_path=db_route.rewrite_path,
        )

