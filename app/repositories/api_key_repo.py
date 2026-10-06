"""API Key repository for database operations."""
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from app.db_models import APIKeyModel
from app.models import APIKey
from datetime import datetime
from app.time_utils import utcnow
from typing import Optional, List
import hashlib
import secrets


class APIKeyRepository:
    """Repository for API key operations."""
    
    @staticmethod
    def _hash_key(key: str) -> str:
        """Hash API key for storage."""
        return hashlib.sha256(key.encode()).hexdigest()
    
    @staticmethod
    async def create(
        db: AsyncSession,
        name: str,
        rate_limit_per_minute: int = 60,
        rate_limit_per_hour: int = 1000,
        allowed_paths: Optional[List[str]] = None,
        expires_at: Optional[datetime] = None,
    ) -> tuple[str, APIKeyModel]:
        """Create a new API key."""
        import uuid
        # Generate new key
        api_key = f"sk-{secrets.token_urlsafe(32)}"
        key_hash = APIKeyRepository._hash_key(api_key)
        key_id = str(uuid.uuid4())
        
        db_key = APIKeyModel(
            id=key_id,
            key=api_key,
            key_hash=key_hash,
            name=name,
            rate_limit_per_minute=rate_limit_per_minute,
            rate_limit_per_hour=rate_limit_per_hour,
            allowed_paths=allowed_paths or [],
            expires_at=expires_at,
            is_active=True,
        )
        
        db.add(db_key)
        await db.commit()
        await db.refresh(db_key)
        
        return api_key, db_key
    
    @staticmethod
    async def get_by_key(db: AsyncSession, key: str) -> Optional[APIKeyModel]:
        """Get API key by key value."""
        key_hash = APIKeyRepository._hash_key(key)
        result = await db.execute(
            select(APIKeyModel).where(APIKeyModel.key_hash == key_hash)
        )
        return result.scalar_one_or_none()
    
    @staticmethod
    async def get_by_id(db: AsyncSession, key_id: str) -> Optional[APIKeyModel]:
        """Get API key by ID."""
        result = await db.execute(
            select(APIKeyModel).where(APIKeyModel.id == key_id)
        )
        return result.scalar_one_or_none()
    
    @staticmethod
    async def list_all(db: AsyncSession, active_only: bool = False) -> List[APIKeyModel]:
        """List all API keys."""
        query = select(APIKeyModel)
        if active_only:
            query = query.where(APIKeyModel.is_active == True)
        result = await db.execute(query)
        return list(result.scalars().all())
    
    @staticmethod
    async def update_last_used(db: AsyncSession, key_id: str):
        """Update last used timestamp."""
        await db.execute(
            update(APIKeyModel)
            .where(APIKeyModel.id == key_id)
            .values(last_used_at=utcnow())
        )
        await db.commit()
    
    @staticmethod
    async def toggle_active(db: AsyncSession, key_id: str) -> Optional[APIKeyModel]:
        """Toggle API key active status."""
        key = await APIKeyRepository.get_by_id(db, key_id)
        if key:
            key.is_active = not key.is_active
            await db.commit()
            await db.refresh(key)
        return key
    
    @staticmethod
    async def delete(db: AsyncSession, key_id: str) -> bool:
        """Delete API key."""
        key = await APIKeyRepository.get_by_id(db, key_id)
        if key:
            await db.delete(key)
            await db.commit()
            return True
        return False
    
    @staticmethod
    def to_pydantic(db_key: APIKeyModel) -> APIKey:
        """Convert database model to Pydantic model."""
        return APIKey(
            key=db_key.key,
            name=db_key.name,
            rate_limit_per_minute=db_key.rate_limit_per_minute,
            rate_limit_per_hour=db_key.rate_limit_per_hour,
            allowed_paths=db_key.allowed_paths or [],
            metadata=db_key.key_metadata or {},
            created_at=db_key.created_at,
            expires_at=db_key.expires_at,
            is_active=db_key.is_active,
        )

