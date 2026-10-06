"""Tests for email notifications and alert thresholds."""
import pytest
from unittest.mock import Mock, patch, AsyncMock
from app.notifications import (
    EmailNotificationService,
    NotificationManager,
    AlertThresholds
)
from app.config import get_settings

settings = get_settings()


@pytest.fixture
def email_service():
    """Create email service for testing."""
    return EmailNotificationService(
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_user="test@example.com",
        smtp_password="password",
        smtp_use_tls=True,
        from_email="noreply@example.com"
    )


@pytest.fixture
def notification_manager():
    """Create notification manager for testing."""
    manager = NotificationManager()
    manager.email_service = Mock(spec=EmailNotificationService)
    manager.email_service.enabled = True
    manager.email_service.send_rate_limit_alert = AsyncMock(return_value=True)
    manager.email_service.send_circuit_breaker_alert = AsyncMock(return_value=True)
    manager.email_service.send_anomaly_alert = AsyncMock(return_value=True)
    manager.email_service.send_api_key_alert = AsyncMock(return_value=True)
    manager.email_service.send_error_rate_alert = AsyncMock(return_value=True)
    manager.email_service.send_traffic_spike_alert = AsyncMock(return_value=True)
    manager.email_service.send_request_error_alert = AsyncMock(return_value=True)
    manager.alert_recipients = ["test@example.com"]
    return manager


class TestEmailNotificationService:
    """Test EmailNotificationService."""
    
    def test_init(self, email_service):
        """Test email service initialization."""
        assert email_service.smtp_host == "smtp.example.com"
        assert email_service.smtp_port == 587
        assert email_service.enabled is True
    
    def test_init_disabled_no_host(self):
        """Test email service disabled when no host."""
        service = EmailNotificationService(smtp_host="")
        assert service.enabled is False
    
    @pytest.mark.asyncio
    async def test_send_email_disabled(self, email_service):
        """Test send email when disabled."""
        email_service.enabled = False
        result = await email_service.send_email(
            ["test@example.com"],
            "Test",
            "Body"
        )
        assert result is False
    
    @pytest.mark.asyncio
    async def test_send_email_no_recipients(self, email_service):
        """Test send email with no recipients."""
        result = await email_service.send_email([], "Test", "Body")
        assert result is False
    
    @pytest.mark.asyncio
    async def test_send_rate_limit_alert(self, email_service):
        """Test send rate limit alert."""
        with patch.object(email_service, 'send_email', new_callable=AsyncMock) as mock_send:
            mock_send.return_value = True
            result = await email_service.send_rate_limit_alert(
                ["test@example.com"],
                "api-key-123",
                "192.168.1.1",
                60,
                "minute",
                "/api/test"
            )
            assert result is True
            mock_send.assert_called_once()
            call_args = mock_send.call_args
            assert "Rate Limit Exceeded" in call_args[0][1]  # subject
    
    @pytest.mark.asyncio
    async def test_send_circuit_breaker_alert(self, email_service):
        """Test send circuit breaker alert."""
        with patch.object(email_service, 'send_email', new_callable=AsyncMock) as mock_send:
            mock_send.return_value = True
            result = await email_service.send_circuit_breaker_alert(
                ["test@example.com"],
                "http://backend:8000",
                "open",
                "closed",
                5,
                "Circuit breaker opened"
            )
            assert result is True
            mock_send.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_send_anomaly_alert(self, email_service):
        """Test send anomaly alert."""
        with patch.object(email_service, 'send_email', new_callable=AsyncMock) as mock_send:
            mock_send.return_value = True
            result = await email_service.send_anomaly_alert(
                ["test@example.com"],
                "client-123",
                "/api/test",
                0.85,
                "req-123",
                "GET",
                "/api/test",
                200,
                150,
                "192.168.1.1"
            )
            assert result is True
            mock_send.assert_called_once()


class TestAlertThresholds:
    """Test AlertThresholds."""
    
    def test_init(self):
        """Test alert thresholds initialization."""
        thresholds = AlertThresholds()
        assert thresholds.rate_limit_enabled is True
        assert thresholds.circuit_breaker_enabled is True
        assert thresholds.anomaly_score_threshold == 0.7
        assert thresholds.error_rate_threshold == 0.1
        assert thresholds.traffic_spike_threshold == 2.0
    
    def test_should_alert_anomaly(self):
        """Test anomaly alert threshold checking."""
        thresholds = AlertThresholds()
        thresholds.anomaly_score_threshold = 0.7
        
        assert thresholds.should_alert_anomaly(0.8) is True
        assert thresholds.should_alert_anomaly(0.7) is True
        assert thresholds.should_alert_anomaly(0.6) is False
    
    def test_should_alert_error_rate(self):
        """Test error rate alert threshold checking."""
        thresholds = AlertThresholds()
        thresholds.error_rate_threshold = 0.1
        
        assert thresholds.should_alert_error_rate(0.15) is True
        assert thresholds.should_alert_error_rate(0.1) is True
        assert thresholds.should_alert_error_rate(0.05) is False
    
    def test_should_alert_traffic_spike(self):
        """Test traffic spike alert threshold checking."""
        thresholds = AlertThresholds()
        thresholds.traffic_spike_threshold = 2.0
        
        assert thresholds.should_alert_traffic_spike(2.5) is True
        assert thresholds.should_alert_traffic_spike(2.0) is True
        assert thresholds.should_alert_traffic_spike(1.5) is False
    
    def test_update(self):
        """Test updating thresholds."""
        thresholds = AlertThresholds()
        thresholds.update(
            anomaly_score_threshold=0.8,
            error_rate_threshold=0.15
        )
        assert thresholds.anomaly_score_threshold == 0.8
        assert thresholds.error_rate_threshold == 0.15
    
    def test_to_dict(self):
        """Test converting thresholds to dictionary."""
        thresholds = AlertThresholds()
        result = thresholds.to_dict()
        assert isinstance(result, dict)
        assert "rate_limit_enabled" in result
        assert "anomaly_score_threshold" in result


class TestNotificationManager:
    """Test NotificationManager."""
    
    def test_init(self, notification_manager):
        """Test notification manager initialization."""
        assert notification_manager.email_service is not None
        assert isinstance(notification_manager.alert_thresholds, AlertThresholds)
    
    def test_set_alert_recipients(self, notification_manager):
        """Test setting alert recipients."""
        notification_manager.set_alert_recipients(["test1@example.com", "test2@example.com"])
        assert len(notification_manager.alert_recipients) == 2
    
    def test_set_alert_recipients_from_string(self, notification_manager):
        """Test setting alert recipients from string."""
        notification_manager.set_alert_recipients_from_string("test1@example.com, test2@example.com")
        assert len(notification_manager.alert_recipients) == 2
        assert "test1@example.com" in notification_manager.alert_recipients
    
    @pytest.mark.asyncio
    async def test_send_rate_limit_alert(self, notification_manager):
        """Test send rate limit alert."""
        notification_manager.alert_thresholds.rate_limit_enabled = True
        notification_manager._check_email_rate_limit = Mock(return_value=True)
        notification_manager._check_alert_interval = Mock(return_value=True)
        notification_manager._record_email_sent = Mock()
        
        await notification_manager.send_rate_limit_alert(
            "api-key-123",
            "192.168.1.1",
            60,
            "minute",
            "/api/test"
        )
        
        notification_manager.email_service.send_rate_limit_alert.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_send_rate_limit_alert_disabled(self, notification_manager):
        """Test send rate limit alert when disabled."""
        notification_manager.alert_thresholds.rate_limit_enabled = False
        
        await notification_manager.send_rate_limit_alert(
            "api-key-123",
            "192.168.1.1",
            60,
            "minute",
            "/api/test"
        )
        
        notification_manager.email_service.send_rate_limit_alert.assert_not_called()
    
    @pytest.mark.asyncio
    async def test_send_rate_limit_alert_rate_limited(self, notification_manager):
        """Test send rate limit alert when email rate limit exceeded."""
        notification_manager.alert_thresholds.rate_limit_enabled = True
        notification_manager._check_email_rate_limit = Mock(return_value=False)
        
        await notification_manager.send_rate_limit_alert(
            "api-key-123",
            "192.168.1.1",
            60,
            "minute",
            "/api/test"
        )
        
        notification_manager.email_service.send_rate_limit_alert.assert_not_called()
    
    @pytest.mark.asyncio
    async def test_send_circuit_breaker_alert(self, notification_manager):
        """Test send circuit breaker alert."""
        notification_manager.alert_thresholds.circuit_breaker_enabled = True
        notification_manager._check_email_rate_limit = Mock(return_value=True)
        notification_manager._check_alert_interval = Mock(return_value=True)
        notification_manager._record_email_sent = Mock()
        
        await notification_manager.send_circuit_breaker_alert(
            "http://backend:8000",
            "open",
            "closed",
            5,
            "Circuit breaker opened"
        )
        
        notification_manager.email_service.send_circuit_breaker_alert.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_send_anomaly_alert(self, notification_manager):
        """Test send anomaly alert."""
        notification_manager.alert_thresholds.anomaly_score_threshold = 0.7
        notification_manager._check_email_rate_limit = Mock(return_value=True)
        notification_manager._check_alert_interval = Mock(return_value=True)
        notification_manager._record_email_sent = Mock()
        
        await notification_manager.send_anomaly_alert(
            "client-123",
            "/api/test",
            0.85,  # Above threshold
            request_id="req-123"
        )
        
        notification_manager.email_service.send_anomaly_alert.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_send_anomaly_alert_below_threshold(self, notification_manager):
        """Test send anomaly alert when score is below threshold."""
        notification_manager.alert_thresholds.anomaly_score_threshold = 0.7
        
        await notification_manager.send_anomaly_alert(
            "client-123",
            "/api/test",
            0.5,  # Below threshold
            request_id="req-123"
        )
        
        notification_manager.email_service.send_anomaly_alert.assert_not_called()
    
    @pytest.mark.asyncio
    async def test_send_api_key_alert(self, notification_manager):
        """Test send API key alert."""
        notification_manager._check_email_rate_limit = Mock(return_value=True)
        notification_manager._check_alert_interval = Mock(return_value=True)
        notification_manager._record_email_sent = Mock()
        
        await notification_manager.send_api_key_alert(
            "created",
            "api-key-123",
            "Test Key",
            "admin-user"
        )
        
        notification_manager.email_service.send_api_key_alert.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_send_error_rate_alert(self, notification_manager):
        """Test send error rate alert."""
        notification_manager.alert_thresholds.error_rate_threshold = 0.1
        notification_manager._check_email_rate_limit = Mock(return_value=True)
        notification_manager._check_alert_interval = Mock(return_value=True)
        notification_manager._record_email_sent = Mock()
        
        await notification_manager.send_error_rate_alert(
            0.15,  # Above threshold
            1000,
            150,
            "5 minutes"
        )
        
        notification_manager.email_service.send_error_rate_alert.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_send_traffic_spike_alert(self, notification_manager):
        """Test send traffic spike alert."""
        notification_manager.alert_thresholds.traffic_spike_threshold = 2.0
        notification_manager._check_email_rate_limit = Mock(return_value=True)
        notification_manager._check_alert_interval = Mock(return_value=True)
        notification_manager._record_email_sent = Mock()
        
        await notification_manager.send_traffic_spike_alert(
            200.0,  # Current RPM
            80.0,   # Baseline RPM
            2.5,    # Multiplier (above threshold)
            "5 minutes"
        )
        
        notification_manager.email_service.send_traffic_spike_alert.assert_called_once()
    
    def test_get_alert_thresholds(self, notification_manager):
        """Test get alert thresholds."""
        thresholds = notification_manager.get_alert_thresholds()
        assert isinstance(thresholds, dict)
        assert "rate_limit_enabled" in thresholds
    
    def test_update_alert_thresholds(self, notification_manager):
        """Test update alert thresholds."""
        notification_manager.update_alert_thresholds(
            anomaly_score_threshold=0.8,
            error_rate_threshold=0.15
        )
        assert notification_manager.alert_thresholds.anomaly_score_threshold == 0.8
        assert notification_manager.alert_thresholds.error_rate_threshold == 0.15
    
    def test_check_email_rate_limit(self, notification_manager):
        """Test email rate limit checking."""
        notification_manager.email_sent_times = []
        notification_manager.email_rate_limit_per_hour = 50
        
        # Should allow when under limit
        assert notification_manager._check_email_rate_limit() is True
        
        # Fill up to limit
        import time
        now = time.time()
        notification_manager.email_sent_times = [now - i for i in range(50)]
        
        # Should block when at limit
        assert notification_manager._check_email_rate_limit() is False
    
    def test_check_alert_interval(self, notification_manager):
        """Test alert interval checking."""
        notification_manager.alert_thresholds.min_interval_seconds = 300
        
        # First alert should pass
        assert notification_manager._check_alert_interval("test:alert") is True
        
        # Second alert immediately should fail
        assert notification_manager._check_alert_interval("test:alert") is False




