"""ML-based anomaly detection for traffic patterns."""
import numpy as np
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
from app.time_utils import utcnow
from collections import deque
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler
import structlog

logger = structlog.get_logger()


class AnomalyDetector:
    """ML-based anomaly detection for API traffic patterns."""
    
    def __init__(self, contamination: float = 0.1, window_size: int = 100):
        """
        Initialize anomaly detector.
        
        Args:
            contamination: Expected proportion of anomalies (0.1 = 10%)
            window_size: Number of recent requests to analyze
        """
        self.contamination = contamination
        self.window_size = window_size
        self.model = IsolationForest(
            contamination=contamination,
            random_state=42,
            n_estimators=100
        )
        self.scaler = StandardScaler()
        self.is_trained = False
        self.request_history = deque(maxlen=window_size * 2)
        self.feature_history = deque(maxlen=window_size)
        
    def _extract_features(self, request_data: Dict) -> np.ndarray:
        """Extract features from request data."""
        features = [
            request_data.get("response_time_ms", 0),
            request_data.get("status_code", 200),
            request_data.get("request_size", 0),
            request_data.get("hour_of_day", 0),
            request_data.get("day_of_week", 0),
            request_data.get("requests_per_minute", 0),
            request_data.get("error_rate", 0),
        ]
        return np.array(features).reshape(1, -1)
    
    def _calculate_derived_features(self, recent_requests: List[Dict]) -> Dict:
        """Calculate derived features from recent requests."""
        if not recent_requests:
            return {
                "requests_per_minute": 0,
                "error_rate": 0,
                "avg_response_time": 0,
            }
        
        now = utcnow()
        one_minute_ago = now - timedelta(minutes=1)
        
        recent = [
            r for r in recent_requests
            if r.get("timestamp") and r["timestamp"] > one_minute_ago
        ]
        
        total_requests = len(recent)
        error_count = sum(1 for r in recent if r.get("status_code", 200) >= 400)
        avg_response_time = (
            sum(r.get("response_time_ms", 0) for r in recent) / total_requests
            if total_requests > 0 else 0
        )
        
        return {
            "requests_per_minute": total_requests,
            "error_rate": error_count / total_requests if total_requests > 0 else 0,
            "avg_response_time": avg_response_time,
        }
    
    def add_request(self, request_data: Dict):
        """Add a request to the history for analysis."""
        timestamp = request_data.get("timestamp", utcnow())
        if isinstance(timestamp, str):
            timestamp = datetime.fromisoformat(timestamp)
        
        enriched_data = {
            **request_data,
            "timestamp": timestamp,
            "hour_of_day": timestamp.hour,
            "day_of_week": timestamp.weekday(),
        }
        
        # Calculate derived features
        recent = list(self.request_history)
        derived = self._calculate_derived_features(recent)
        enriched_data.update(derived)
        
        self.request_history.append(enriched_data)
        
        # Extract features for ML model
        features = self._extract_features(enriched_data)
        self.feature_history.append(features.flatten())
    
    def train(self):
        """Train the anomaly detection model on historical data."""
        if len(self.feature_history) < self.window_size:
            logger.warning(
                "insufficient_data_for_training",
                available=len(self.feature_history),
                required=self.window_size
            )
            return False
        
        # Prepare training data
        X = np.array(list(self.feature_history))
        
        # Scale features
        X_scaled = self.scaler.fit_transform(X)
        
        # Train model
        self.model.fit(X_scaled)
        self.is_trained = True
        
        logger.info("anomaly_detector_trained", samples=len(X))
        return True
    
    def detect_anomaly(self, request_data: Dict) -> Tuple[bool, float]:
        """
        Detect if a request is anomalous.
        
        Returns:
            (is_anomaly, anomaly_score)
            - is_anomaly: True if request is anomalous
            - anomaly_score: Score from -1 (normal) to 1 (anomalous)
        """
        if not self.is_trained:
            # Auto-train if we have enough data
            if len(self.feature_history) >= self.window_size:
                try:
                    self.train()
                    # Check if training was successful
                    if not self.is_trained:
                        return False, 0.0
                except Exception as e:
                    logger.warning("anomaly_detector_training_failed", error=str(e))
                    return False, 0.0
            else:
                return False, 0.0
        
        try:
            # Extract features
            features = self._extract_features(request_data)
            
            # Check if scaler has been fitted
            if not hasattr(self.scaler, 'mean_') or self.scaler.mean_ is None:
                logger.warning("anomaly_detector_scaler_not_fitted")
                return False, 0.0
            
            features_scaled = self.scaler.transform(features)
            
            # Validate scaled features
            if features_scaled is None or len(features_scaled) == 0:
                logger.warning("anomaly_detector_invalid_features")
                return False, 0.0
            
            # Predict
            prediction_result = self.model.predict(features_scaled)
            score_result = self.model.score_samples(features_scaled)
            
            # Validate prediction results
            if prediction_result is None or len(prediction_result) == 0:
                logger.warning("anomaly_detector_invalid_prediction")
                return False, 0.0
            
            if score_result is None or len(score_result) == 0:
                logger.warning("anomaly_detector_invalid_score")
                return False, 0.0
            
            prediction = prediction_result[0]
            score = score_result[0]
            
            # Normalize score to -1 (normal) to 1 (anomalous)
            # IsolationForest returns negative scores for anomalies
            normalized_score = -score / 10.0  # Normalize
            normalized_score = max(-1.0, min(1.0, normalized_score))
            
            is_anomaly = bool(prediction == -1)
            
            if is_anomaly:
                logger.warning(
                    "anomaly_detected",
                    path=request_data.get("path"),
                    score=normalized_score,
                    features=features.flatten().tolist()
                )
            
            return is_anomaly, float(normalized_score)
        except Exception as e:
            logger.warning("anomaly_detection_error", error=str(e), error_type=type(e).__name__)
            return False, 0.0
    
    def detect_anomalies(self, batch: List[Dict]) -> List[Tuple[bool, float]]:
        """``detect_anomaly`` for many requests with one model call.

        IsolationForest's ``predict`` is ``score_samples - offset_ < 0``, so one
        ``score_samples`` over the batch gives both answers ``detect_anomaly``
        gives for each request, at the cost of about one call instead of two
        per request.
        """
        nothing = [(False, 0.0)] * len(batch)
        if not batch:
            return []
        if not self.is_trained:
            if len(self.feature_history) < self.window_size:
                return nothing
            try:
                self.train()
            except Exception as e:
                logger.warning("anomaly_detector_training_failed", error=str(e))
                return nothing
            if not self.is_trained:
                return nothing
        try:
            if not hasattr(self.scaler, 'mean_') or self.scaler.mean_ is None:
                logger.warning("anomaly_detector_scaler_not_fitted")
                return nothing
            features = np.vstack([self._extract_features(r) for r in batch])
            scores = self.model.score_samples(self.scaler.transform(features))
            decisions = scores - self.model.offset_
            results: List[Tuple[bool, float]] = []
            for request_data, row, score, decision in zip(batch, features, scores, decisions):
                normalized_score = max(-1.0, min(1.0, -float(score) / 10.0))
                is_anomaly = bool(decision < 0)
                if is_anomaly:
                    logger.warning(
                        "anomaly_detected",
                        path=request_data.get("path"),
                        score=normalized_score,
                        features=row.tolist(),
                    )
                results.append((is_anomaly, normalized_score))
            return results
        except Exception as e:
            logger.warning("anomaly_detection_error", error=str(e), error_type=type(e).__name__)
            return nothing

    def get_anomaly_summary(self, window_minutes: int = 60) -> Dict:
        """Get summary of anomalies in the last window."""
        cutoff = utcnow() - timedelta(minutes=window_minutes)
        
        recent = [
            r for r in self.request_history
            if r.get("timestamp") and r["timestamp"] > cutoff
        ]
        
        if not recent:
            return {
                "total_requests": 0,
                "anomalies": 0,
                "anomaly_rate": 0.0,
                "top_anomalous_paths": [],
            }
        
        # Check each request
        anomalies = []
        for req in recent:
            is_anomaly, score = self.detect_anomaly(req)
            if is_anomaly:
                anomalies.append({
                    "path": req.get("path"),
                    "score": score,
                    "timestamp": req.get("timestamp").isoformat() if req.get("timestamp") else None,
                })
        
        # Group by path
        path_counts = {}
        for anomaly in anomalies:
            path = anomaly["path"]
            path_counts[path] = path_counts.get(path, 0) + 1
        
        top_paths = sorted(
            path_counts.items(),
            key=lambda x: x[1],
            reverse=True
        )[:5]
        
        return {
            "total_requests": len(recent),
            "anomalies": len(anomalies),
            "anomaly_rate": len(anomalies) / len(recent) if recent else 0.0,
            "top_anomalous_paths": [
                {"path": path, "count": count}
                for path, count in top_paths
            ],
            "recent_anomalies": anomalies[-10:],  # Last 10 anomalies
        }


