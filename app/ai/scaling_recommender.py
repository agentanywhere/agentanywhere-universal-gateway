"""AI-powered auto-scaling recommendations."""
from typing import Dict, List, Optional
from datetime import datetime, timedelta
from app.time_utils import utcnow
from collections import deque
import structlog

logger = structlog.get_logger()


class ScalingRecommender:
    """AI system that recommends optimal scaling based on traffic patterns."""
    
    def __init__(self):
        """Initialize scaling recommender."""
        self.metrics_history = deque(maxlen=1000)
        self.current_recommendations = {}
        
    def add_metrics(self, metrics: Dict):
        """Add current system metrics."""
        timestamp = metrics.get("timestamp", utcnow())
        if isinstance(timestamp, str):
            timestamp = datetime.fromisoformat(timestamp)
        
        self.metrics_history.append({
            **metrics,
            "timestamp": timestamp,
        })
    
    def _analyze_traffic_trend(self, window_minutes: int = 60) -> Dict:
        """Analyze traffic trends over time window."""
        cutoff = utcnow() - timedelta(minutes=window_minutes)
        
        recent = [
            m for m in self.metrics_history
            if m.get("timestamp") and m["timestamp"] > cutoff
        ]
        
        if len(recent) < 10:
            return {
                "trend": "stable",
                "growth_rate": 0.0,
                "volatility": 0.0,
            }
        
        # Extract request counts
        requests = [m.get("requests_per_minute", 0) for m in recent]
        
        # Calculate trend
        if len(requests) >= 2:
            first_half = requests[:len(requests)//2]
            second_half = requests[len(requests)//2:]
            
            avg_first = sum(first_half) / len(first_half)
            avg_second = sum(second_half) / len(second_half)
            
            if avg_first > 0:
                growth_rate = (avg_second - avg_first) / avg_first
            else:
                growth_rate = 0.0
            
            # Determine trend
            if growth_rate > 0.2:
                trend = "increasing"
            elif growth_rate < -0.2:
                trend = "decreasing"
            else:
                trend = "stable"
            
            # Calculate volatility (coefficient of variation)
            if requests:
                mean = sum(requests) / len(requests)
                variance = sum((x - mean) ** 2 for x in requests) / len(requests)
                std_dev = variance ** 0.5
                volatility = std_dev / mean if mean > 0 else 0.0
            else:
                volatility = 0.0
        else:
            trend = "stable"
            growth_rate = 0.0
            volatility = 0.0
        
        return {
            "trend": trend,
            "growth_rate": growth_rate,
            "volatility": volatility,
        }
    
    def _calculate_utilization(self, metrics: Dict) -> float:
        """Calculate current system utilization."""
        current_rpm = metrics.get("requests_per_minute", 0)
        max_capacity = metrics.get("max_capacity_rpm", 1000)
        
        if max_capacity > 0:
            utilization = current_rpm / max_capacity
        else:
            utilization = 0.0
        
        return min(1.0, max(0.0, utilization))
    
    def get_scaling_recommendation(self, current_metrics: Dict) -> Dict:
        """
        Get scaling recommendation based on current metrics.
        
        Returns:
            Recommendation with action, reason, and confidence
        """
        # Analyze trends
        trend_analysis = self._analyze_traffic_trend()
        
        # Calculate utilization
        utilization = self._calculate_utilization(current_metrics)
        
        # Get recent metrics
        recent = list(self.metrics_history)[-10:] if len(self.metrics_history) >= 10 else list(self.metrics_history)
        
        if not recent:
            return {
                "action": "maintain",
                "reason": "Insufficient data",
                "confidence": 0.0,
                "current_utilization": utilization,
            }
        
        # Calculate average metrics
        avg_rpm = sum(m.get("requests_per_minute", 0) for m in recent) / len(recent)
        avg_latency = sum(m.get("avg_latency_ms", 0) for m in recent) / len(recent)
        avg_error_rate = sum(m.get("error_rate", 0) for m in recent) / len(recent)
        
        # Decision logic
        action = "maintain"
        reason = ""
        confidence = 0.5
        
        # Scale up conditions
        if utilization > 0.8:
            action = "scale_up"
            reason = f"High utilization ({utilization:.1%}). Current: {avg_rpm:.0f} req/min"
            confidence = 0.9
        elif utilization > 0.7 and trend_analysis["trend"] == "increasing":
            action = "scale_up"
            reason = f"Rising utilization ({utilization:.1%}) with increasing traffic trend"
            confidence = 0.8
        elif avg_latency > 500 and utilization > 0.6:
            action = "scale_up"
            reason = f"High latency ({avg_latency:.0f}ms) with moderate utilization"
            confidence = 0.75
        elif avg_error_rate > 0.05 and utilization > 0.7:
            action = "scale_up"
            reason = f"High error rate ({avg_error_rate:.1%}) with high utilization"
            confidence = 0.8
        
        # Scale down conditions
        elif utilization < 0.3 and trend_analysis["trend"] == "stable":
            action = "scale_down"
            reason = f"Low utilization ({utilization:.1%}) with stable traffic"
            confidence = 0.7
        elif utilization < 0.2:
            action = "scale_down"
            reason = f"Very low utilization ({utilization:.1%})"
            confidence = 0.9
        
        # Calculate recommended instance count
        current_instances = current_metrics.get("current_instances", 1)
        target_utilization = 0.6  # Target 60% utilization
        
        if avg_rpm > 0:
            recommended_instances = int(
                (avg_rpm / current_metrics.get("max_capacity_rpm", 1000)) / target_utilization
            )
            recommended_instances = max(1, recommended_instances)
        else:
            recommended_instances = current_instances
        
        # Cost estimate
        cost_per_instance = current_metrics.get("cost_per_instance_per_hour", 0.1)
        current_cost = current_instances * cost_per_instance
        recommended_cost = recommended_instances * cost_per_instance
        cost_change = recommended_cost - current_cost
        
        recommendation = {
            "action": action,
            "reason": reason or "No action needed",
            "confidence": confidence,
            "current_utilization": utilization,
            "current_instances": current_instances,
            "recommended_instances": recommended_instances,
            "traffic_trend": trend_analysis["trend"],
            "growth_rate": trend_analysis["growth_rate"],
            "current_cost_per_hour": current_cost,
            "recommended_cost_per_hour": recommended_cost,
            "cost_change_per_hour": cost_change,
            "estimated_savings_per_month": cost_change * 24 * 30 if cost_change < 0 else 0,
            "metrics": {
                "avg_requests_per_minute": avg_rpm,
                "avg_latency_ms": avg_latency,
                "avg_error_rate": avg_error_rate,
            },
        }
        
        self.current_recommendations["latest"] = recommendation
        
        logger.info(
            "scaling_recommendation",
            action=action,
            confidence=confidence,
            utilization=utilization,
        )
        
        return recommendation
    
    def get_scaling_history(self, hours: int = 24) -> List[Dict]:
        """Get scaling recommendation history."""
        cutoff = utcnow() - timedelta(hours=hours)
        
        return [
            rec for rec in self.current_recommendations.values()
            if rec.get("timestamp") and rec["timestamp"] > cutoff
        ]


