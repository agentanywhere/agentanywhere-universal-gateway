"""ML-based cost optimization and analysis."""
from typing import Dict, List, Optional
from datetime import timedelta
from app.time_utils import utcnow
from collections import defaultdict, deque
import structlog

logger = structlog.get_logger()


class CostOptimizer:
    """AI-powered cost optimization and analysis."""
    
    def __init__(self):
        """Initialize cost optimizer."""
        self.cost_history = deque(maxlen=1000)
        self.usage_by_endpoint = defaultdict(lambda: {"requests": 0, "cost": 0.0})
        self.optimization_suggestions = []
        
    def record_request_cost(
        self,
        endpoint: str,
        request_cost: float,
        response_time_ms: float,
        status_code: int
    ):
        """Record cost for a request."""
        self.usage_by_endpoint[endpoint]["requests"] += 1
        self.usage_by_endpoint[endpoint]["cost"] += request_cost
        
        self.cost_history.append({
            "timestamp": utcnow(),
            "endpoint": endpoint,
            "cost": request_cost,
            "response_time_ms": response_time_ms,
            "status_code": status_code,
        })
    
    def _calculate_endpoint_efficiency(self, endpoint: str) -> Dict:
        """Calculate cost efficiency for an endpoint."""
        usage = self.usage_by_endpoint.get(endpoint, {"requests": 0, "cost": 0.0})
        
        if usage["requests"] == 0:
            return {
                "efficiency_score": 0.0,
                "cost_per_request": 0.0,
                "recommendations": [],
            }
        
        cost_per_request = usage["cost"] / usage["requests"]
        
        # Get recent requests for this endpoint
        recent = [
            r for r in self.cost_history
            if r.get("endpoint") == endpoint
            and r.get("timestamp")
            and r["timestamp"] > utcnow() - timedelta(hours=24)
        ]
        
        if not recent:
            return {
                "efficiency_score": 0.5,
                "cost_per_request": cost_per_request,
                "recommendations": [],
            }
        
        # Calculate metrics
        avg_response_time = sum(r.get("response_time_ms", 0) for r in recent) / len(recent)
        error_rate = sum(1 for r in recent if r.get("status_code", 200) >= 400) / len(recent)
        
        # Efficiency score (0-1, higher is better)
        # Based on: low cost, low latency, low error rate
        cost_score = max(0.0, 1.0 - (cost_per_request * 100))  # Normalize
        latency_score = max(0.0, 1.0 - (avg_response_time / 1000))  # Normalize to 1s
        error_score = 1.0 - error_rate
        
        efficiency_score = (cost_score * 0.4 + latency_score * 0.4 + error_score * 0.2)
        
        # Generate recommendations
        recommendations = []
        
        if cost_per_request > 0.01:  # $0.01 per request
            recommendations.append(
                f"High cost per request (${cost_per_request:.4f}). Consider caching or optimization."
            )
        
        if avg_response_time > 500:
            recommendations.append(
                f"High latency ({avg_response_time:.0f}ms). Consider backend optimization."
            )
        
        if error_rate > 0.05:
            recommendations.append(
                f"High error rate ({error_rate:.1%}). Failed requests still incur costs."
            )
        
        return {
            "efficiency_score": efficiency_score,
            "cost_per_request": cost_per_request,
            "avg_response_time_ms": avg_response_time,
            "error_rate": error_rate,
            "recommendations": recommendations,
        }
    
    def analyze_costs(self, window_hours: int = 24) -> Dict:
        """Analyze costs over time window."""
        cutoff = utcnow() - timedelta(hours=window_hours)
        
        recent = [
            r for r in self.cost_history
            if r.get("timestamp") and r["timestamp"] > cutoff
        ]
        
        if not recent:
            return {
                "total_cost": 0.0,
                "total_requests": 0,
                "avg_cost_per_request": 0.0,
                "cost_by_endpoint": {},
                "requests_by_endpoint": {},
                "optimization_opportunities": [],
            }
        
        total_cost = sum(r.get("cost", 0) for r in recent)
        total_requests = len(recent)
        avg_cost_per_request = total_cost / total_requests if total_requests > 0 else 0.0
        
        # Cost by endpoint
        cost_by_endpoint = defaultdict(float)
        requests_by_endpoint = defaultdict(int)
        
        for r in recent:
            endpoint = r.get("endpoint", "unknown")
            cost_by_endpoint[endpoint] += r.get("cost", 0)
            requests_by_endpoint[endpoint] += 1
        
        # Find optimization opportunities
        optimization_opportunities = []
        
        # Top cost endpoints
        top_cost_endpoints = sorted(
            cost_by_endpoint.items(),
            key=lambda x: x[1],
            reverse=True
        )[:5]
        
        for endpoint, cost in top_cost_endpoints:
            efficiency = self._calculate_endpoint_efficiency(endpoint)
            if efficiency["efficiency_score"] < 0.6:
                optimization_opportunities.append({
                    "endpoint": endpoint,
                    "current_cost": cost,
                    "efficiency_score": efficiency["efficiency_score"],
                    "recommendations": efficiency["recommendations"],
                    "potential_savings": cost * 0.2,  # Estimate 20% savings
                })
        
        # Calculate trends
        if len(recent) >= 2:
            first_half = recent[:len(recent)//2]
            second_half = recent[len(recent)//2:]
            
            cost_first = sum(r.get("cost", 0) for r in first_half)
            cost_second = sum(r.get("cost", 0) for r in second_half)
            
            if cost_first > 0:
                cost_trend = (cost_second - cost_first) / cost_first
            else:
                cost_trend = 0.0
        else:
            cost_trend = 0.0
        
        return {
            "total_cost": total_cost,
            "total_requests": total_requests,
            "avg_cost_per_request": avg_cost_per_request,
            "cost_by_endpoint": dict(cost_by_endpoint),
            "requests_by_endpoint": dict(requests_by_endpoint),
            "cost_trend": cost_trend,
            "optimization_opportunities": optimization_opportunities,
            "estimated_monthly_cost": total_cost * (30 * 24 / window_hours),
            "potential_monthly_savings": sum(
                opp["potential_savings"] for opp in optimization_opportunities
            ) * (30 * 24 / window_hours),
        }
    
    def get_optimization_recommendations(self) -> List[Dict]:
        """Get actionable cost optimization recommendations."""
        analysis = self.analyze_costs()
        
        recommendations = []
        
        # High cost endpoints
        if analysis["optimization_opportunities"]:
            for opp in analysis["optimization_opportunities"][:3]:
                recommendations.append({
                    "type": "endpoint_optimization",
                    "priority": "high",
                    "endpoint": opp["endpoint"],
                    "current_cost": opp["current_cost"],
                    "potential_savings": opp["potential_savings"],
                    "actions": opp["recommendations"],
                })
        
        # Caching opportunities
        top_endpoints = sorted(
            analysis["requests_by_endpoint"].items(),
            key=lambda x: x[1],
            reverse=True
        )[:5]
        
        for endpoint, requests in top_endpoints:
            if requests > 1000:  # High traffic
                recommendations.append({
                    "type": "caching",
                    "priority": "medium",
                    "endpoint": endpoint,
                    "requests": requests,
                    "potential_savings": requests * 0.001 * 0.5,  # Estimate 50% cache hit
                    "actions": [
                        f"Implement caching for {endpoint}",
                        "Expected cache hit rate: 50-70%",
                    ],
                })
        
        # Rate limiting optimization
        if analysis["total_requests"] > 10000:
            recommendations.append({
                "type": "rate_limiting",
                "priority": "medium",
                "description": "High request volume detected",
                "potential_savings": analysis["total_cost"] * 0.1,
                "actions": [
                    "Review and optimize rate limits",
                    "Consider tiered rate limiting",
                ],
            })
        
        return recommendations

