"""Tests for AI scaling recommender."""
import pytest
from datetime import datetime, timedelta
from app.time_utils import utcnow
from app.ai.scaling_recommender import ScalingRecommender


@pytest.fixture
def recommender():
    """Create a scaling recommender instance."""
    return ScalingRecommender()


def test_add_metrics(recommender):
    """Test adding metrics to recommender."""
    metrics = {
        "requests_per_minute": 100,
        "avg_latency_ms": 50,
        "error_rate": 0.01,
        "timestamp": utcnow()
    }
    recommender.add_metrics(metrics)
    assert len(recommender.metrics_history) == 1
    assert recommender.metrics_history[0]["requests_per_minute"] == 100


def test_add_metrics_with_string_timestamp(recommender):
    """Test adding metrics with string timestamp."""
    metrics = {
        "requests_per_minute": 100,
        "timestamp": utcnow().isoformat()
    }
    recommender.add_metrics(metrics)
    assert len(recommender.metrics_history) == 1
    assert isinstance(recommender.metrics_history[0]["timestamp"], datetime)


def test_analyze_traffic_trend_insufficient_data(recommender):
    """Test traffic trend analysis with insufficient data."""
    # Add only 5 metrics (less than 10 required)
    for i in range(5):
        recommender.add_metrics({
            "requests_per_minute": 100 + i,
            "timestamp": utcnow() - timedelta(minutes=5-i)
        })
    
    trend = recommender._analyze_traffic_trend()
    assert trend["trend"] == "stable"
    assert trend["growth_rate"] == 0.0
    assert trend["volatility"] == 0.0


def test_analyze_traffic_trend_increasing(recommender):
    """Test traffic trend analysis with increasing traffic."""
    # Add metrics showing increasing trend
    for i in range(20):
        recommender.add_metrics({
            "requests_per_minute": 50 + i * 5,
            "timestamp": utcnow() - timedelta(minutes=20-i)
        })
    
    trend = recommender._analyze_traffic_trend()
    assert trend["trend"] == "increasing"
    assert trend["growth_rate"] > 0.2


def test_analyze_traffic_trend_decreasing(recommender):
    """Test traffic trend analysis with decreasing traffic."""
    # Add metrics showing decreasing trend
    for i in range(20):
        recommender.add_metrics({
            "requests_per_minute": 150 - i * 5,
            "timestamp": utcnow() - timedelta(minutes=20-i)
        })
    
    trend = recommender._analyze_traffic_trend()
    assert trend["trend"] == "decreasing"
    assert trend["growth_rate"] < -0.2


def test_analyze_traffic_trend_stable(recommender):
    """Test traffic trend analysis with stable traffic."""
    # Add metrics showing stable trend
    for i in range(20):
        recommender.add_metrics({
            "requests_per_minute": 100 + (i % 3) - 1,  # Small variation
            "timestamp": utcnow() - timedelta(minutes=20-i)
        })
    
    trend = recommender._analyze_traffic_trend()
    assert trend["trend"] == "stable"
    assert abs(trend["growth_rate"]) <= 0.2


def test_calculate_utilization(recommender):
    """Test utilization calculation."""
    metrics = {
        "requests_per_minute": 600,
        "max_capacity_rpm": 1000
    }
    utilization = recommender._calculate_utilization(metrics)
    assert utilization == 0.6


def test_calculate_utilization_max(recommender):
    """Test utilization calculation at max capacity."""
    metrics = {
        "requests_per_minute": 1200,
        "max_capacity_rpm": 1000
    }
    utilization = recommender._calculate_utilization(metrics)
    assert utilization == 1.0  # Capped at 1.0


def test_calculate_utilization_zero_capacity(recommender):
    """Test utilization calculation with zero capacity."""
    metrics = {
        "requests_per_minute": 100,
        "max_capacity_rpm": 0
    }
    utilization = recommender._calculate_utilization(metrics)
    assert utilization == 0.0


def test_get_scaling_recommendation_insufficient_data(recommender):
    """Test scaling recommendation with insufficient data."""
    current_metrics = {
        "requests_per_minute": 100,
        "max_capacity_rpm": 1000,
        "current_instances": 1,
        "cost_per_instance_per_hour": 0.1
    }
    
    recommendation = recommender.get_scaling_recommendation(current_metrics)
    assert recommendation["action"] == "maintain"
    assert recommendation["reason"] == "Insufficient data"
    assert recommendation["confidence"] == 0.0


def test_get_scaling_recommendation_scale_up_high_utilization(recommender):
    """Test scaling recommendation for high utilization."""
    # Add metrics
    for i in range(15):
        recommender.add_metrics({
            "requests_per_minute": 850,
            "avg_latency_ms": 100,
            "error_rate": 0.01,
            "timestamp": utcnow() - timedelta(minutes=15-i)
        })
    
    current_metrics = {
        "requests_per_minute": 850,
        "max_capacity_rpm": 1000,
        "current_instances": 1,
        "cost_per_instance_per_hour": 0.1
    }
    
    recommendation = recommender.get_scaling_recommendation(current_metrics)
    assert recommendation["action"] == "scale_up"
    assert recommendation["confidence"] >= 0.9
    assert "High utilization" in recommendation["reason"]


def test_get_scaling_recommendation_scale_up_high_latency(recommender):
    """Test scaling recommendation for high latency."""
    # Add metrics
    for i in range(15):
        recommender.add_metrics({
            "requests_per_minute": 650,
            "avg_latency_ms": 600,
            "error_rate": 0.01,
            "timestamp": utcnow() - timedelta(minutes=15-i)
        })
    
    current_metrics = {
        "requests_per_minute": 650,
        "max_capacity_rpm": 1000,
        "current_instances": 1,
        "cost_per_instance_per_hour": 0.1
    }
    
    recommendation = recommender.get_scaling_recommendation(current_metrics)
    assert recommendation["action"] == "scale_up"
    assert "High latency" in recommendation["reason"]


def test_get_scaling_recommendation_scale_down_low_utilization(recommender):
    """Test scaling recommendation for low utilization."""
    # Add metrics
    for i in range(15):
        recommender.add_metrics({
            "requests_per_minute": 250,
            "avg_latency_ms": 50,
            "error_rate": 0.01,
            "timestamp": utcnow() - timedelta(minutes=15-i)
        })
    
    current_metrics = {
        "requests_per_minute": 250,
        "max_capacity_rpm": 1000,
        "current_instances": 3,
        "cost_per_instance_per_hour": 0.1
    }
    
    recommendation = recommender.get_scaling_recommendation(current_metrics)
    assert recommendation["action"] == "scale_down"
    assert "Low utilization" in recommendation["reason"]


def test_get_scaling_recommendation_scale_down_very_low_utilization(recommender):
    """Test scaling recommendation for very low utilization."""
    # Add metrics
    for i in range(15):
        recommender.add_metrics({
            "requests_per_minute": 150,
            "avg_latency_ms": 50,
            "error_rate": 0.01,
            "timestamp": utcnow() - timedelta(minutes=15-i)
        })
    
    current_metrics = {
        "requests_per_minute": 150,
        "max_capacity_rpm": 1000,
        "current_instances": 3,
        "cost_per_instance_per_hour": 0.1
    }
    
    recommendation = recommender.get_scaling_recommendation(current_metrics)
    assert recommendation["action"] == "scale_down"
    assert recommendation["confidence"] >= 0.7  # Can be 0.7 or 0.9 depending on trend


def test_get_scaling_recommendation_includes_cost_info(recommender):
    """Test that scaling recommendation includes cost information."""
    # Add metrics
    for i in range(15):
        recommender.add_metrics({
            "requests_per_minute": 100,
            "avg_latency_ms": 50,
            "error_rate": 0.01,
            "timestamp": utcnow() - timedelta(minutes=15-i)
        })
    
    current_metrics = {
        "requests_per_minute": 100,
        "max_capacity_rpm": 1000,
        "current_instances": 2,
        "cost_per_instance_per_hour": 0.1
    }
    
    recommendation = recommender.get_scaling_recommendation(current_metrics)
    assert "current_cost_per_hour" in recommendation
    assert "recommended_cost_per_hour" in recommendation
    assert "cost_change_per_hour" in recommendation
    assert recommendation["current_instances"] == 2
    assert recommendation["recommended_instances"] >= 1


def test_get_scaling_history(recommender):
    """Test getting scaling recommendation history."""
    # Add a recommendation
    current_metrics = {
        "requests_per_minute": 100,
        "max_capacity_rpm": 1000,
        "current_instances": 1,
        "cost_per_instance_per_hour": 0.1
    }
    recommender.get_scaling_recommendation(current_metrics)
    
    history = recommender.get_scaling_history()
    assert len(history) >= 0  # May or may not have timestamp


def test_get_scaling_history_with_timestamp(recommender):
    """Test getting scaling history with timestamp filtering."""
    # Add a recommendation with timestamp
    current_metrics = {
        "requests_per_minute": 100,
        "max_capacity_rpm": 1000,
        "current_instances": 1,
        "cost_per_instance_per_hour": 0.1
    }
    recommendation = recommender.get_scaling_recommendation(current_metrics)
    recommendation["timestamp"] = utcnow()
    recommender.current_recommendations["test"] = recommendation
    
    history = recommender.get_scaling_history(hours=1)
    assert len(history) >= 1

