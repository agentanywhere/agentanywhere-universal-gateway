"""Request log repository for database operations."""
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, and_, func
from app.db_models import RequestLogModel
from app.models import RequestLog
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
from app.time_utils import utcnow


class LogRepository:
    """Repository for request log operations."""
    
    @staticmethod
    async def create(
        db: AsyncSession,
        request_log: RequestLog,
        api_key_id: Optional[str] = None,
    ) -> RequestLogModel:
        """Create a new request log entry."""
        db_log = RequestLogModel(
            request_id=request_log.request_id,
            method=request_log.method,
            path=request_log.path,
            client_ip=request_log.client_ip,
            api_key_id=api_key_id,
            status_code=request_log.status_code,
            response_time_ms=int(request_log.response_time_ms),
            user_agent=request_log.user_agent,
            error=request_log.error,
            protocol=getattr(request_log, 'protocol', None),
            timestamp=request_log.timestamp if hasattr(request_log, 'timestamp') else utcnow(),
        )
        
        db.add(db_log)
        await db.commit()
        await db.refresh(db_log)
        return db_log
    
    @staticmethod
    async def create_many(db: AsyncSession, items) -> int:
        """Insert many ``(RequestLog, api_key_id)`` pairs in one transaction, without the
        refresh that ``create`` does (nothing reads the rows back)."""
        rows = [
            RequestLogModel(
                request_id=log.request_id,
                method=log.method,
                path=log.path,
                client_ip=log.client_ip,
                api_key_id=key_id,
                status_code=log.status_code,
                response_time_ms=int(log.response_time_ms),
                user_agent=log.user_agent,
                error=log.error,
                protocol=getattr(log, "protocol", None),
                timestamp=log.timestamp if hasattr(log, "timestamp") else utcnow(),
            )
            for log, key_id in items
        ]
        db.add_all(rows)
        await db.commit()
        return len(rows)

    @staticmethod
    async def get_by_request_id(db: AsyncSession, request_id: str) -> Optional[RequestLogModel]:
        """Get log by request ID."""
        result = await db.execute(
            select(RequestLogModel).where(RequestLogModel.request_id == request_id)
        )
        return result.scalar_one_or_none()
    
    @staticmethod
    async def list_logs(
        db: AsyncSession,
        limit: int = 100,
        offset: int = 0,
        level: Optional[str] = None,
        path: Optional[str] = None,
        status_code: Optional[int] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> tuple[List[RequestLogModel], int]:
        """List logs with filtering."""
        query = select(RequestLogModel)
        count_query = select(func.count()).select_from(RequestLogModel)
        
        conditions = []
        
        if level:
            if level == "error":
                conditions.append(RequestLogModel.status_code >= 500)
            elif level == "warning":
                conditions.append(
                    and_(
                        RequestLogModel.status_code >= 400,
                        RequestLogModel.status_code < 500
                    )
                )
        
        if path:
            conditions.append(RequestLogModel.path.like(f"%{path}%"))
        
        if status_code:
            conditions.append(RequestLogModel.status_code == status_code)
        
        if start_date:
            conditions.append(RequestLogModel.timestamp >= start_date)
        
        if end_date:
            conditions.append(RequestLogModel.timestamp <= end_date)
        
        if conditions:
            query = query.where(and_(*conditions))
            count_query = count_query.where(and_(*conditions))
        
        # Get total count
        total_result = await db.execute(count_query)
        total = total_result.scalar() or 0
        
        # Get paginated results
        result = await db.execute(
            query.order_by(desc(RequestLogModel.timestamp))
            .limit(limit)
            .offset(offset)
        )
        logs = list(result.scalars().all())
        
        return logs, total
    
    @staticmethod
    async def get_stats(
        db: AsyncSession,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Get aggregated statistics."""
        from sqlalchemy import func, case
        
        query = select(
            func.count(RequestLogModel.id).label("total_requests"),
            func.sum(
                case((RequestLogModel.status_code >= 400, 1), else_=0)
            ).label("total_errors"),
            func.avg(RequestLogModel.response_time_ms).label("avg_response_time"),
        )
        
        if start_date:
            query = query.where(RequestLogModel.timestamp >= start_date)
        if end_date:
            query = query.where(RequestLogModel.timestamp <= end_date)
        
        result = await db.execute(query)
        row = result.first()
        
        return {
            "total_requests": row.total_requests or 0,
            "total_errors": row.total_errors or 0,
            "avg_response_time": float(row.avg_response_time or 0),
        }
    
    @staticmethod
    async def cleanup_old_logs(db: AsyncSession, days: int = 30):
        """Delete logs older than specified days."""
        from sqlalchemy import delete
        cutoff_date = utcnow() - timedelta(days=days)
        await db.execute(
            delete(RequestLogModel).where(RequestLogModel.timestamp < cutoff_date)
        )
        await db.commit()

