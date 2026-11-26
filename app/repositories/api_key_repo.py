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

from app import api_key_cache

# Characters of a key kept for display ("sk-AbCdEfGh…"). Enough to tell keys
# apart in a list, far too few to use.
KEY_PREFIX_LENGTH = 11
# Stored in key_metadata, so no schema change is needed for hashed storage.
_PREFIX_META = "key_prefix"


class APIKeyRepository:
    """Repository for API key operations.

    Keys are stored as their SHA-256 hash (``key_hash``) and looked up by it.
    The ``key`` column is no longer written: the full key is returned once,
    by :meth:`create`, and never again. Rows written by earlier releases still
    hold the key in ``key``; they keep working (they have the same hash), and
    the plaintext copy is removed the first time the key is used.
    """
    
    @staticmethod
    def _hash_key(key: str) -> str:
        """Hash API key for storage."""
        return api_key_cache.hash_key(key)
    
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
            key=None,  # only the hash is stored
            key_hash=key_hash,
            name=name,
            rate_limit_per_minute=rate_limit_per_minute,
            rate_limit_per_hour=rate_limit_per_hour,
            allowed_paths=allowed_paths or [],
            key_metadata={_PREFIX_META: APIKeyRepository.display_prefix(api_key)},
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
    async def mark_used(db: AsyncSession, db_key: APIKeyModel) -> None:
        """Record a successful use; migrate a legacy plaintext row to hash-only.

        The caller has just matched the key by its hash, so dropping the
        plaintext copy loses nothing. The display prefix is kept first.

        ``last_used_at`` is written at most once per
        ``API_KEY_LAST_USED_INTERVAL_SECONDS`` for a key (0 writes on every use):
        a write and commit on every request made the database the bottleneck,
        SQLite above all. A legacy row is migrated on its first use regardless.
        """
        now = utcnow()
        if not db_key.key and APIKeyRepository._used_recently(db_key.last_used_at, now):
            return
        values = {"last_used_at": now}
        if db_key.key:
            meta = dict(db_key.key_metadata or {})
            meta.setdefault(_PREFIX_META, APIKeyRepository.display_prefix(db_key.key))
            values["key"] = None
            values["key_metadata"] = meta
            # Drop any cache entry an older release keyed by the plaintext.
            api_key_cache.invalidate(None, plaintext=db_key.key)
        await db.execute(
            update(APIKeyModel).where(APIKeyModel.id == db_key.id).values(**values)
        )
        await db.commit()
        for field, value in values.items():
            try:
                setattr(db_key, field, value)
            except Exception:
                pass

    @staticmethod
    def _used_recently(last_used: Optional[datetime], now: datetime) -> bool:
        """True when ``last_used`` is within the last-used interval before ``now``."""
        from app.config import get_settings

        interval = float(getattr(get_settings(), "api_key_last_used_interval_seconds", 0) or 0)
        if interval <= 0 or last_used is None:
            return False
        if (last_used.tzinfo is None) != (now.tzinfo is None):
            # SQLite hands back naive datetimes; compare both as naive UTC.
            last_used, now = last_used.replace(tzinfo=None), now.replace(tzinfo=None)
        age = (now - last_used).total_seconds()
        return 0 <= age < interval

    @staticmethod
    async def toggle_active(db: AsyncSession, key_id: str) -> Optional[APIKeyModel]:
        """Toggle API key active status."""
        key = await APIKeyRepository.get_by_id(db, key_id)
        if key:
            key.is_active = not key.is_active
            await db.commit()
            await db.refresh(key)
            # A disabled key must stop working now, not when the cache expires.
            api_key_cache.invalidate(key.key_hash, plaintext=key.key)
        return key

    @staticmethod
    async def delete(db: AsyncSession, key_id: str) -> bool:
        """Delete API key."""
        key = await APIKeyRepository.get_by_id(db, key_id)
        if key:
            key_hash, plaintext = key.key_hash, key.key
            await db.delete(key)
            await db.commit()
            api_key_cache.invalidate(key_hash, plaintext=plaintext)
            return True
        return False

    @staticmethod
    def display_prefix(key: Optional[str]) -> str:
        return (key or "")[:KEY_PREFIX_LENGTH]

    @staticmethod
    def public_metadata(db_key: APIKeyModel) -> dict:
        """The key's metadata without the gateway's own bookkeeping."""
        meta = dict(db_key.key_metadata or {})
        meta.pop(_PREFIX_META, None)
        return meta

    @staticmethod
    def to_public_dict(db_key: APIKeyModel) -> dict:
        """What the admin API may show about a key: never the key itself."""
        meta = db_key.key_metadata or {}
        prefix = meta.get(_PREFIX_META) or APIKeyRepository.display_prefix(db_key.key)
        return {
            "id": db_key.id,
            "name": db_key.name,
            "key_prefix": prefix,
            "rate_limit_per_minute": db_key.rate_limit_per_minute,
            "rate_limit_per_hour": db_key.rate_limit_per_hour,
            "allowed_paths": db_key.allowed_paths or [],
            "metadata": APIKeyRepository.public_metadata(db_key),
            "created_at": db_key.created_at.isoformat() if db_key.created_at else None,
            "expires_at": db_key.expires_at.isoformat() if db_key.expires_at else None,
            "last_used_at": db_key.last_used_at.isoformat() if db_key.last_used_at else None,
            "is_active": db_key.is_active,
        }

    @staticmethod
    def to_pydantic(db_key: APIKeyModel, presented_key: Optional[str] = None) -> APIKey:
        """Convert database model to the request principal.

        ``presented_key`` is the key the caller sent; it is the principal's
        identity (rate limits, tenant lookup). Rows no longer store the key,
        so without it the identity falls back to the hash.
        """
        return APIKey(
            id=db_key.id,
            key=presented_key or db_key.key or f"sha256:{db_key.key_hash}",
            name=db_key.name,
            rate_limit_per_minute=db_key.rate_limit_per_minute,
            rate_limit_per_hour=db_key.rate_limit_per_hour,
            allowed_paths=db_key.allowed_paths or [],
            metadata=APIKeyRepository.public_metadata(db_key),
            created_at=db_key.created_at,
            expires_at=db_key.expires_at,
            is_active=db_key.is_active,
        )

