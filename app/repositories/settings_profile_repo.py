"""Settings profile repository for database operations."""
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete
from app.db_models import SettingsProfileModel
from app.models import SettingsProfile, ProtocolSettings
from typing import List, Optional
from app.time_utils import utcnow
import uuid
import json


class SettingsProfileRepository:
    """Repository for settings profile operations."""
    
    @staticmethod
    async def create(
        db: AsyncSession,
        profile: SettingsProfile,
        created_by: Optional[str] = None,
    ) -> SettingsProfileModel:
        """Create a new settings profile."""
        profile_id = profile.id or str(uuid.uuid4())
        
        # If this is set as default, unset other defaults
        if profile.is_default:
            await SettingsProfileRepository.unset_all_defaults(db)
        
        db_profile = SettingsProfileModel(
            id=profile_id,
            name=profile.name,
            description=profile.description,
            protocol_settings={
                protocol: settings.dict() if isinstance(settings, ProtocolSettings) else settings
                for protocol, settings in profile.protocol_settings.items()
            },
            global_settings=profile.global_settings,
            is_default=profile.is_default,
            created_by=created_by,
        )
        
        db.add(db_profile)
        await db.commit()
        await db.refresh(db_profile)
        return db_profile
    
    @staticmethod
    async def get_by_id(db: AsyncSession, profile_id: str) -> Optional[SettingsProfileModel]:
        """Get profile by ID."""
        result = await db.execute(
            select(SettingsProfileModel).where(SettingsProfileModel.id == profile_id)
        )
        return result.scalar_one_or_none()
    
    @staticmethod
    async def get_by_name(db: AsyncSession, name: str) -> Optional[SettingsProfileModel]:
        """Get profile by name."""
        result = await db.execute(
            select(SettingsProfileModel).where(SettingsProfileModel.name == name)
        )
        return result.scalar_one_or_none()
    
    @staticmethod
    async def get_default(db: AsyncSession) -> Optional[SettingsProfileModel]:
        """Get the default profile."""
        result = await db.execute(
            select(SettingsProfileModel).where(SettingsProfileModel.is_default == True)
        )
        return result.scalar_one_or_none()
    
    @staticmethod
    async def list_all(db: AsyncSession) -> List[SettingsProfileModel]:
        """List all profiles."""
        result = await db.execute(
            select(SettingsProfileModel).order_by(
                SettingsProfileModel.is_default.desc(),
                SettingsProfileModel.created_at.desc()
            )
        )
        return list(result.scalars().all())
    
    @staticmethod
    async def update(
        db: AsyncSession,
        profile_id: str,
        profile: SettingsProfile,
    ) -> Optional[SettingsProfileModel]:
        """Update a profile."""
        # If setting as default, unset others
        if profile.is_default:
            await SettingsProfileRepository.unset_all_defaults(db, exclude_id=profile_id)
        
        await db.execute(
            update(SettingsProfileModel)
            .where(SettingsProfileModel.id == profile_id)
            .values(
                name=profile.name,
                description=profile.description,
                protocol_settings={
                    protocol: settings.dict() if isinstance(settings, ProtocolSettings) else settings
                    for protocol, settings in profile.protocol_settings.items()
                },
                global_settings=profile.global_settings,
                is_default=profile.is_default,
                updated_at=utcnow(),
            )
        )
        await db.commit()
        return await SettingsProfileRepository.get_by_id(db, profile_id)
    
    @staticmethod
    async def delete(db: AsyncSession, profile_id: str) -> bool:
        """Delete a profile."""
        result = await db.execute(
            delete(SettingsProfileModel).where(SettingsProfileModel.id == profile_id)
        )
        await db.commit()
        return result.rowcount > 0
    
    @staticmethod
    async def unset_all_defaults(db: AsyncSession, exclude_id: Optional[str] = None):
        """Unset all default flags except the excluded ID."""
        query = update(SettingsProfileModel).values(is_default=False)
        if exclude_id:
            query = query.where(SettingsProfileModel.id != exclude_id)
        await db.execute(query)
        await db.commit()
    
    @staticmethod
    async def set_default(db: AsyncSession, profile_id: str) -> Optional[SettingsProfileModel]:
        """Set a profile as default."""
        await SettingsProfileRepository.unset_all_defaults(db, exclude_id=profile_id)
        await db.execute(
            update(SettingsProfileModel)
            .where(SettingsProfileModel.id == profile_id)
            .values(is_default=True)
        )
        await db.commit()
        return await SettingsProfileRepository.get_by_id(db, profile_id)


