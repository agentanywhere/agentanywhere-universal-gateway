"""AI service integration for the gateway."""
from typing import Dict
from app.ai.anomaly_detector import AnomalyDetector
from app.ai.intelligent_rate_limiter import IntelligentRateLimiter
from app.ai.scaling_recommender import ScalingRecommender
from app.ai.cost_optimizer import CostOptimizer
from app.ai.predictive_analytics import PredictiveAnalytics
from app.config import get_settings
from app.webhooks import webhook_manager, WebhookEventType
import structlog
import asyncio

logger = structlog.get_logger()
settings = get_settings()


class AIService:
    """Centralized AI service for all AI-powered features."""
    
    def __init__(self):
        """Initialize AI service with all AI components."""
        self.anomaly_detector = AnomalyDetector(contamination=0.1)
        self.intelligent_rate_limiter = IntelligentRateLimiter(
            base_limit_per_minute=settings.rate_limit_per_minute
        )
        self.scaling_recommender = ScalingRecommender()
        self.cost_optimizer = CostOptimizer()
        self.predictive_analytics = PredictiveAnalytics()
        
        # Auto-train models periodically
        self._training_enabled = True
        
        logger.info("ai_service_initialized")
    
    def process_request(self, request_data: Dict):
        """Process a request through all AI systems."""
        client_id = request_data.get("client_id") or request_data.get("api_key", "unknown")
        # Ensure client_id is a string and safe to slice
        if not isinstance(client_id, str):
            client_id = str(client_id) if client_id else "unknown"
        endpoint = request_data.get("path", "unknown")
        
        # Anomaly detection
        is_anomaly, anomaly_score = self.anomaly_detector.detect_anomaly(request_data)
        if is_anomaly:
            logger.warning(
                "anomaly_detected",
                client_id=client_id[:8] if len(client_id) >= 8 else client_id,
                endpoint=endpoint,
                score=anomaly_score,
            )
            # Trigger webhook for anomaly detection
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.create_task(
                        webhook_manager.trigger_webhook(
                            WebhookEventType.ANOMALY_DETECTED,
                            {
                                "client_id": client_id,
                                "endpoint": endpoint,
                                "anomaly_score": anomaly_score,
                                "request_id": request_data.get("request_id"),
                                "method": request_data.get("method"),
                                "path": request_data.get("path"),
                                "status_code": request_data.get("status_code"),
                                "response_time_ms": request_data.get("response_time_ms"),
                                "client_ip": request_data.get("client_ip"),
                                "timestamp": request_data.get("timestamp"),
                                "message": f"Anomaly detected for {endpoint} with score {anomaly_score:.2f}"
                            }
                        )
                    )
                    # Send email notification
                    from app.notifications import notification_manager
                    asyncio.create_task(
                        notification_manager.send_anomaly_alert(
                            client_id=client_id,
                            endpoint=endpoint,
                            anomaly_score=anomaly_score,
                            request_id=request_data.get("request_id"),
                            method=request_data.get("method"),
                            path=request_data.get("path"),
                            status_code=request_data.get("status_code"),
                            response_time_ms=request_data.get("response_time_ms"),
                            client_ip=request_data.get("client_ip")
                        )
                    )
            except (RuntimeError, AttributeError):
                # No event loop running, webhook will be skipped
                pass
        
        # Add to history for analysis
        self.anomaly_detector.add_request(request_data)
        
        # Intelligent rate limiting
        self.intelligent_rate_limiter.add_request(client_id, request_data)
        
        # Cost tracking
        request_cost = self._calculate_request_cost(request_data)
        self.cost_optimizer.record_request_cost(
            endpoint=endpoint,
            request_cost=request_cost,
            response_time_ms=request_data.get("response_time_ms", 0),
            status_code=request_data.get("status_code", 200),
        )
        
        # Predictive analytics
        self.predictive_analytics.add_metric({
            "requests_per_minute": 1,  # Will be aggregated
            "status_code": request_data.get("status_code", 200),
            "response_time_ms": request_data.get("response_time_ms", 0),
            "timestamp": request_data.get("timestamp"),
        })
        
        return {
            "is_anomaly": is_anomaly,
            "anomaly_score": anomaly_score,
            "adaptive_rate_limit": self.intelligent_rate_limiter.get_adaptive_limit(client_id),
            "request_cost": request_cost,
        }
    
    def _calculate_request_cost(self, request_data: Dict) -> float:
        """Calculate cost for a request."""
        # Simplified cost model
        # Base cost per request
        base_cost = 0.0001  # $0.0001 per request
        
        # Additional cost for compute time
        response_time_ms = request_data.get("response_time_ms", 0)
        compute_cost = (response_time_ms / 1000) * 0.00001  # $0.00001 per second
        
        # Error cost (wasted resources)
        if request_data.get("status_code", 200) >= 400:
            error_cost = base_cost * 0.5  # 50% of base cost wasted
        else:
            error_cost = 0.0
        
        return base_cost + compute_cost + error_cost
    
    def get_ai_insights(self) -> Dict:
        """Get comprehensive AI insights."""
        # Anomaly summary
        anomaly_summary = self.anomaly_detector.get_anomaly_summary()
        
        # Cost analysis
        cost_analysis = self.cost_optimizer.analyze_costs()
        
        # Scaling recommendations
        current_metrics = {
            "requests_per_minute": sum(
                m.get("requests_per_minute", 0)
                for m in list(self.predictive_analytics.metrics_history)[-10:]
            ) / 10 if len(self.predictive_analytics.metrics_history) >= 10 else 0,
            "avg_latency_ms": 100,  # Would come from actual metrics
            "error_rate": 0.01,
            "current_instances": 1,
            "max_capacity_rpm": 1000,
            "cost_per_instance_per_hour": 0.1,
        }
        scaling_rec = self.scaling_recommender.get_scaling_recommendation(current_metrics)
        
        # Traffic forecast
        traffic_forecast = self.predictive_analytics.predict_traffic(hours_ahead=24)
        
        # Cost optimization recommendations
        cost_recommendations = self.cost_optimizer.get_optimization_recommendations()
        
        return {
            "anomalies": anomaly_summary,
            "cost_analysis": cost_analysis,
            "scaling_recommendation": scaling_rec,
            "traffic_forecast": traffic_forecast[:12],  # Next 12 hours
            "cost_optimization": cost_recommendations,
            "intelligent_rate_limiting": {
                "active_clients": len(self.intelligent_rate_limiter.client_profiles),
                "adaptive_limits_active": len(self.intelligent_rate_limiter.adaptive_limits),
            },
        }
    
    def train_models(self):
        """Train all AI models."""
        logger.info("training_ai_models")
        
        # Train anomaly detector
        self.anomaly_detector.train()
        
        # Train predictive analytics
        self.predictive_analytics.train()
        
        logger.info("ai_models_trained")

