"""Intelligent adaptive rate limiting based on behavior analysis."""
from typing import Dict, Optional
from datetime import datetime, timedelta
from app.time_utils import utcnow
from collections import defaultdict, deque
import structlog

logger = structlog.get_logger()


class IntelligentRateLimiter:
    """Adaptive rate limiting that adjusts based on traffic patterns."""
    
    def __init__(self, base_limit_per_minute: int = 60):
        """
        Initialize intelligent rate limiter.
        
        Args:
            base_limit_per_minute: Base rate limit per minute
        """
        self.base_limit = base_limit_per_minute
        self.client_profiles = {}  # Client behavior profiles
        self.request_history = defaultdict(lambda: deque(maxlen=1000))
        self.adaptive_limits = {}  # Current adaptive limits per client
        
    def _calculate_client_profile(self, client_id: str) -> Dict:
        """Calculate behavior profile for a client."""
        history = list(self.request_history[client_id])
        if len(history) < 10:
            return {
                "avg_requests_per_minute": self.base_limit,
                "burst_factor": 1.0,
                "error_rate": 0.0,
                "trust_score": 0.5,  # Neutral trust
            }
        
        # Analyze last hour
        now = utcnow()
        one_hour_ago = now - timedelta(hours=1)
        
        recent = [
            r for r in history
            if r.get("timestamp") and r["timestamp"] > one_hour_ago
        ]
        
        if not recent:
            return {
                "avg_requests_per_minute": self.base_limit,
                "burst_factor": 1.0,
                "error_rate": 0.0,
                "trust_score": 0.5,
            }
        
        # Calculate metrics
        total_requests = len(recent)
        error_count = sum(1 for r in recent if r.get("status_code", 200) >= 400)
        error_rate = error_count / total_requests if total_requests > 0 else 0
        
        # Calculate requests per minute
        minutes = max(1, (now - one_hour_ago).total_seconds() / 60)
        avg_rpm = total_requests / minutes
        
        # Calculate burst factor (ratio of peak to average)
        requests_by_minute = defaultdict(int)
        for r in recent:
            if r.get("timestamp"):
                minute_key = r["timestamp"].replace(second=0, microsecond=0)
                requests_by_minute[minute_key] += 1
        
        if requests_by_minute:
            peak_rpm = max(requests_by_minute.values())
            burst_factor = peak_rpm / avg_rpm if avg_rpm > 0 else 1.0
        else:
            burst_factor = 1.0
        
        # Calculate trust score (0-1)
        # Higher trust = lower error rate, consistent usage
        trust_score = max(0.0, min(1.0, 1.0 - error_rate * 2))
        if avg_rpm < self.base_limit * 0.5:
            trust_score = min(1.0, trust_score + 0.2)  # Bonus for low usage
        
        return {
            "avg_requests_per_minute": avg_rpm,
            "burst_factor": burst_factor,
            "error_rate": error_rate,
            "trust_score": trust_score,
        }
    
    def add_request(self, client_id: str, request_data: Dict):
        """Record a request for analysis."""
        timestamp = request_data.get("timestamp", utcnow())
        if isinstance(timestamp, str):
            timestamp = datetime.fromisoformat(timestamp)
        
        self.request_history[client_id].append({
            **request_data,
            "timestamp": timestamp,
        })
        
        # Update profile
        self.client_profiles[client_id] = self._calculate_client_profile(client_id)
    
    def get_adaptive_limit(self, client_id: str) -> int:
        """
        Get adaptive rate limit for a client.
        
        Returns:
            Adaptive rate limit per minute
        """
        profile = self.client_profiles.get(client_id)
        
        if not profile:
            return self.base_limit
        
        trust_score = profile["trust_score"]
        avg_rpm = profile["avg_requests_per_minute"]
        burst_factor = profile["burst_factor"]
        
        # Calculate adaptive limit
        # Trusted clients get higher limits
        # Limit is based on historical usage + trust bonus
        base_multiplier = 1.0 + (trust_score - 0.5) * 0.5  # 0.75x to 1.25x
        
        # Account for burst patterns
        burst_allowance = min(2.0, 1.0 + burst_factor * 0.3)
        
        # Calculate limit
        adaptive_limit = int(
            max(
                self.base_limit * 0.5,  # Minimum 50% of base
                min(
                    self.base_limit * 2.0,  # Maximum 200% of base
                    avg_rpm * base_multiplier * burst_allowance
                )
            )
        )
        
        # Cache the limit
        self.adaptive_limits[client_id] = adaptive_limit
        
        logger.debug(
            "adaptive_limit_calculated",
            client_id=client_id[:8],
            limit=adaptive_limit,
            trust_score=trust_score,
            avg_rpm=avg_rpm,
        )
        
        return adaptive_limit
    
    def should_throttle(self, client_id: str, current_count: int) -> bool:
        """Check if client should be throttled."""
        limit = self.get_adaptive_limit(client_id)
        return current_count >= limit
    
    def get_recommendations(self, client_id: str) -> Dict:
        """Get rate limiting recommendations for a client."""
        profile = self.client_profiles.get(client_id)
        
        if not profile:
            return {
                "current_limit": self.base_limit,
                "recommended_limit": self.base_limit,
                "reason": "Insufficient data",
            }
        
        current_limit = self.adaptive_limits.get(client_id, self.base_limit)
        recommended_limit = self.get_adaptive_limit(client_id)
        
        recommendations = []
        
        if profile["error_rate"] > 0.1:
            recommendations.append(
                "High error rate detected. Consider reducing request rate."
            )
        
        if profile["burst_factor"] > 2.0:
            recommendations.append(
                "Bursty traffic pattern. Consider implementing request queuing."
            )
        
        if profile["trust_score"] < 0.3:
            recommendations.append(
                "Low trust score. Monitor for potential abuse."
            )
        
        return {
            "current_limit": current_limit,
            "recommended_limit": recommended_limit,
            "trust_score": profile["trust_score"],
            "avg_requests_per_minute": profile["avg_requests_per_minute"],
            "error_rate": profile["error_rate"],
            "recommendations": recommendations,
        }


