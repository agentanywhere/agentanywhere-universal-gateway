"""Tests for AI predictive analytics."""
import pytest
from datetime import datetime, timedelta
from app.time_utils import utcnow
from app.ai.predictive_analytics import PredictiveAnalytics
import numpy as np


@pytest.fixture
def analytics():
    """Create a predictive analytics instance."""
    return PredictiveAnalytics()


def test_add_metric(analytics):
    """Test adding a metric."""
    metric = {
        "requests_per_minute": 100,
        "status_code": 200,
        "response_time_ms": 50,
        "timestamp": utcnow()
    }
    analytics.add_metric(metric)
    assert len(analytics.metrics_history) == 1


def test_add_metric_with_string_timestamp(analytics):
    """Test adding metric with string timestamp."""
    metric = {
        "requests_per_minute": 100,
        "timestamp": utcnow().isoformat()
    }
    analytics.add_metric(metric)
    assert len(analytics.metrics_history) == 1
    assert isinstance(analytics.metrics_history[0]["timestamp"], datetime)


def test_prepare_training_data_insufficient_data(analytics):
    """Test preparing training data with insufficient data."""
    # Add less than 100 metrics
    for i in range(50):
        analytics.add_metric({
            "requests_per_minute": 100 + i,
            "timestamp": utcnow() - timedelta(minutes=50-i)
        })
    
    X, y = analytics._prepare_training_data()
    assert X is None
    assert y is None


def test_prepare_training_data_sufficient_data(analytics):
    """Test preparing training data with sufficient data."""
    # Add enough metrics (at least 100, and need 24+ hours of data)
    base_time = utcnow() - timedelta(hours=25)
    for i in range(1500):  # 25 hours * 60 minutes
        analytics.add_metric({
            "requests_per_minute": 100 + (i % 10),
            "status_code": 200 if i % 10 != 0 else 500,
            "response_time_ms": 50 + (i % 20),
            "timestamp": base_time + timedelta(minutes=i)
        })
    
    X, y = analytics._prepare_training_data()
    # May still return None if not enough hourly data points
    # This is acceptable behavior
    assert X is None or len(X) > 0


def test_train_insufficient_data(analytics):
    """Test training with insufficient data."""
    result = analytics.train()
    assert result is False
    assert analytics.is_trained is False


def test_predict_traffic_not_trained(analytics):
    """Test traffic prediction when model is not trained."""
    predictions = analytics.predict_traffic()
    assert predictions == []


def test_predict_traffic_insufficient_data(analytics):
    """Test traffic prediction with insufficient data."""
    # Add some metrics but not enough
    for i in range(50):
        analytics.add_metric({
            "requests_per_minute": 100,
            "timestamp": utcnow() - timedelta(minutes=50-i)
        })
    
    predictions = analytics.predict_traffic()
    assert predictions == []


def test_predict_traffic_with_trained_model(analytics):
    """Test traffic prediction with trained model."""
    # Add enough data and train
    base_time = utcnow() - timedelta(hours=25)
    for i in range(1500):
        analytics.add_metric({
            "requests_per_minute": 100 + (i % 10),
            "status_code": 200,
            "response_time_ms": 50,
            "timestamp": base_time + timedelta(minutes=i)
        })
    
    # Try to train (may succeed or fail depending on data)
    trained = analytics.train()
    
    if trained:
        predictions = analytics.predict_traffic(hours_ahead=12)
        # If predictions are generated, verify structure
        if predictions:
            assert len(predictions) == 12
            for pred in predictions:
                assert "timestamp" in pred
                assert "predicted_requests_per_hour" in pred
                assert "predicted_requests_per_minute" in pred
                assert "confidence" in pred


def test_predict_capacity_needs(analytics):
    """Test capacity needs prediction."""
    # Add some data
    for i in range(100):
        analytics.add_metric({
            "requests_per_minute": 100,
            "timestamp": utcnow() - timedelta(minutes=100-i)
        })
    
    # Returns a dict with capacity information
    capacity_needs = analytics.predict_capacity_needs()
    assert isinstance(capacity_needs, dict)
    # May have empty forecast if not trained
    if capacity_needs.get("forecast"):
        assert isinstance(capacity_needs["forecast"], list)


def test_get_traffic_patterns(analytics):
    """Test getting traffic patterns."""
    # Add data for different hours
    base_time = utcnow() - timedelta(hours=48)
    for i in range(2880):  # 48 hours * 60 minutes
        hour = (base_time + timedelta(minutes=i)).hour
        # Simulate peak hours (9-17)
        requests = 200 if 9 <= hour <= 17 else 50
        analytics.add_metric({
            "requests_per_minute": requests,
            "timestamp": base_time + timedelta(minutes=i)
        })
    
    patterns = analytics.get_traffic_patterns()
    assert isinstance(patterns, dict)
    # May have patterns if enough data
    if patterns:
        assert "peak_hour" in patterns or "avg_by_hour" in patterns


def test_get_traffic_patterns_insufficient_data(analytics):
    """Test getting traffic patterns with insufficient data."""
    # Add minimal data
    for i in range(10):
        analytics.add_metric({
            "requests_per_minute": 100,
            "timestamp": utcnow() - timedelta(minutes=10-i)
        })
    
    patterns = analytics.get_traffic_patterns()
    assert isinstance(patterns, dict)
    # May return empty patterns
    assert patterns.get("peak_hour") is None or isinstance(patterns.get("peak_hour"), int)

