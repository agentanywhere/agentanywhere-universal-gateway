"""Predictive analytics for traffic and capacity forecasting."""
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
from app.time_utils import utcnow
from collections import defaultdict, deque
import numpy as np
from sklearn.linear_model import LinearRegression
from sklearn.preprocessing import PolynomialFeatures
import structlog

logger = structlog.get_logger()


class PredictiveAnalytics:
    """Predictive analytics for traffic forecasting and capacity planning."""
    
    def __init__(self, history_days: int = 7):
        """
        Initialize predictive analytics.
        
        Args:
            history_days: Number of days of history to use for predictions
        """
        self.history_days = history_days
        self.metrics_history = deque(maxlen=history_days * 24 * 60)  # Per-minute data
        self.model = LinearRegression()
        self.poly_features = PolynomialFeatures(degree=2)
        self.is_trained = False
        
    def add_metric(self, metric: Dict):
        """Add a metric data point."""
        timestamp = metric.get("timestamp", utcnow())
        if isinstance(timestamp, str):
            timestamp = datetime.fromisoformat(timestamp)
        
        self.metrics_history.append({
            **metric,
            "timestamp": timestamp,
        })
    
    def _prepare_training_data(self, hours_ahead: int = 24) -> Tuple[np.ndarray, np.ndarray]:
        """Prepare training data for prediction model."""
        if len(self.metrics_history) < 100:
            return None, None
        
        # Group by hour
        hourly_data = {}
        for metric in self.metrics_history:
            timestamp = metric.get("timestamp")
            if not timestamp:
                continue
            
            hour_key = timestamp.replace(minute=0, second=0, microsecond=0)
            if hour_key not in hourly_data:
                hourly_data[hour_key] = {
                    "requests": 0,
                    "errors": 0,
                    "latency": [],
                }
            
            hourly_data[hour_key]["requests"] += metric.get("requests_per_minute", 0)
            if metric.get("status_code", 200) >= 400:
                hourly_data[hour_key]["errors"] += 1
            if metric.get("response_time_ms"):
                hourly_data[hour_key]["latency"].append(metric["response_time_ms"])
        
        # Convert to arrays
        sorted_hours = sorted(hourly_data.keys())
        
        if len(sorted_hours) < 24:
            return None, None
        
        X = []
        y = []
        
        for i in range(len(sorted_hours) - hours_ahead):
            hour = sorted_hours[i]
            data = hourly_data[hour]
            
            # Features: hour of day, day of week, requests, errors, avg latency
            features = [
                hour.hour,
                hour.weekday(),
                data["requests"],
                data["errors"],
                sum(data["latency"]) / len(data["latency"]) if data["latency"] else 0,
            ]
            
            # Target: requests in future hour
            future_hour = sorted_hours[i + min(hours_ahead, len(sorted_hours) - i - 1)]
            future_data = hourly_data[future_hour]
            target = future_data["requests"]
            
            X.append(features)
            y.append(target)
        
        return np.array(X), np.array(y)
    
    def train(self):
        """Train the prediction model."""
        X, y = self._prepare_training_data()
        
        if X is None or len(X) < 24:
            logger.warning("insufficient_data_for_training")
            return False
        
        # Use polynomial features for non-linear patterns
        X_poly = self.poly_features.fit_transform(X)
        
        # Train model
        self.model.fit(X_poly, y)
        self.is_trained = True
        
        logger.info("predictive_model_trained", samples=len(X))
        return True
    
    def predict_traffic(
        self,
        hours_ahead: int = 24,
        include_confidence: bool = True
    ) -> List[Dict]:
        """
        Predict traffic for the next N hours.
        
        Returns:
            List of predictions with timestamps and confidence intervals
        """
        if not self.is_trained:
            if len(self.metrics_history) >= 100:
                self.train()
            else:
                return []
        
        # Get recent data for context
        recent = list(self.metrics_history)[-60:] if len(self.metrics_history) >= 60 else list(self.metrics_history)
        
        if not recent:
            return []
        
        # Calculate baseline from recent data
        recent_requests = [m.get("requests_per_minute", 0) for m in recent]
        baseline_rpm = sum(recent_requests) / len(recent_requests) if recent_requests else 0
        
        predictions = []
        now = utcnow()
        
        for hour_offset in range(1, hours_ahead + 1):
            future_time = now + timedelta(hours=hour_offset)
            
            # Prepare features
            features = np.array([[
                future_time.hour,
                future_time.weekday(),
                baseline_rpm,
                0,  # errors (unknown)
                0,  # latency (unknown)
            ]])
            
            # Predict
            try:
                X_poly = self.poly_features.transform(features)
                prediction_result = self.model.predict(X_poly)
                
                # Validate prediction result
                if prediction_result is None or len(prediction_result) == 0:
                    # Use baseline if prediction fails
                    prediction = baseline_rpm
                else:
                    prediction = prediction_result[0]
                
                # Calculate confidence (simplified - based on model fit)
                # In production, use prediction intervals
                confidence_lower = max(0, prediction * 0.8)
                confidence_upper = prediction * 1.2
            except Exception as e:
                logger.warning("predictive_analytics_prediction_error", error=str(e))
                # Fallback to baseline
                prediction = baseline_rpm
                confidence_lower = max(0, prediction * 0.8)
                confidence_upper = prediction * 1.2
            
            predictions.append({
                "timestamp": future_time.isoformat(),
                "predicted_requests_per_hour": max(0, int(prediction * 60)),
                "predicted_requests_per_minute": max(0, int(prediction)),
                "confidence_lower": max(0, int(confidence_lower * 60)),
                "confidence_upper": int(confidence_upper * 60),
                "confidence": 0.7,  # Simplified confidence score
            })
        
        return predictions
    
    def predict_capacity_needs(
        self,
        hours_ahead: int = 24,
        target_utilization: float = 0.7
    ) -> Dict:
        """Predict capacity needs based on traffic forecast."""
        traffic_forecast = self.predict_traffic(hours_ahead)
        
        if not traffic_forecast:
            return {
                "recommended_capacity": 0,
                "current_capacity": 0,
                "forecast": [],
            }
        
        # Get peak predicted traffic
        peak_prediction = max(
            p["predicted_requests_per_minute"] for p in traffic_forecast
        )
        
        # Calculate required capacity
        # Assuming each instance can handle 1000 req/min at target utilization
        capacity_per_instance = 1000
        required_capacity = peak_prediction / (capacity_per_instance * target_utilization)
        recommended_instances = max(1, int(np.ceil(required_capacity)))
        
        return {
            "recommended_capacity": recommended_instances,
            "current_capacity": 1,  # Would come from actual metrics
            "peak_predicted_rpm": peak_prediction,
            "target_utilization": target_utilization,
            "forecast": traffic_forecast,
            "recommendation": (
                "scale_up" if recommended_instances > 1
                else "maintain"
            ),
        }
    
    def get_traffic_patterns(self) -> Dict:
        """Analyze traffic patterns and trends."""
        if len(self.metrics_history) < 100:
            return {
                "patterns": [],
                "trends": {},
            }
        
        # Group by hour of day
        hourly_patterns = defaultdict(list)
        daily_patterns = defaultdict(list)
        
        for metric in self.metrics_history:
            timestamp = metric.get("timestamp")
            if not timestamp:
                continue
            
            hour = timestamp.hour
            weekday = timestamp.weekday()
            requests = metric.get("requests_per_minute", 0)
            
            hourly_patterns[hour].append(requests)
            daily_patterns[weekday].append(requests)
        
        # Calculate averages
        avg_by_hour = {
            hour: sum(requests) / len(requests)
            for hour, requests in hourly_patterns.items()
        }
        
        avg_by_day = {
            day: sum(requests) / len(requests)
            for day, requests in daily_patterns.items()
        }
        
        # Find peak hours
        peak_hour = max(avg_by_hour.items(), key=lambda x: x[1])[0] if avg_by_hour else None
        peak_day = max(avg_by_day.items(), key=lambda x: x[1])[0] if avg_by_day else None
        
        return {
            "hourly_patterns": avg_by_hour,
            "daily_patterns": avg_by_day,
            "peak_hour": peak_hour,
            "peak_day": peak_day,
            "patterns": [
                {
                    "type": "hourly_peak",
                    "hour": peak_hour,
                    "avg_requests": avg_by_hour.get(peak_hour, 0),
                },
                {
                    "type": "daily_peak",
                    "day": peak_day,
                    "avg_requests": avg_by_day.get(peak_day, 0),
                },
            ],
        }

