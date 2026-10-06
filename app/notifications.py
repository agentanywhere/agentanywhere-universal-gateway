"""Email, Slack, and other notification services."""
from typing import List, Optional, Dict, Any
from app.time_utils import utcnow
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import httpx
from app.config import get_settings
import structlog
import asyncio
import time
from collections import defaultdict

logger = structlog.get_logger()
settings = get_settings()


class EmailNotificationService:
    """Email notification service."""
    
    def __init__(
        self,
        smtp_host: str,
        smtp_port: int = 587,
        smtp_user: Optional[str] = None,
        smtp_password: Optional[str] = None,
        smtp_use_tls: bool = True,
        from_email: Optional[str] = None,
        from_name: str = "AgentAnywhere Universal Gateway"
    ):
        """Initialize email notification service."""
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_user = smtp_user
        self.smtp_password = smtp_password
        self.smtp_use_tls = smtp_use_tls
        self.from_email = from_email or smtp_user
        self.from_name = from_name
        self.enabled = bool(smtp_host)
    
    async def send_email(
        self,
        to_emails: List[str],
        subject: str,
        body: str,
        html_body: Optional[str] = None
    ) -> bool:
        """Send email notification."""
        if not self.enabled:
            logger.warning("email_notification_disabled")
            return False
        
        if not to_emails:
            logger.warning("no_email_recipients")
            return False
        
        try:
            # Create message
            msg = MIMEMultipart('alternative')
            msg['From'] = f"{self.from_name} <{self.from_email}>"
            msg['To'] = ", ".join(to_emails)
            msg['Subject'] = subject
            
            # Add text and HTML parts
            text_part = MIMEText(body, 'plain')
            msg.attach(text_part)
            
            if html_body:
                html_part = MIMEText(html_body, 'html')
                msg.attach(html_part)
            
            # Send email
            await asyncio.to_thread(self._send_smtp, msg, to_emails)
            
            logger.info("email_sent", to=to_emails, subject=subject)
            return True
        
        except Exception as e:
            logger.error("email_send_failed", error=str(e), to=to_emails)
            return False
    
    def _send_smtp(self, msg: MIMEMultipart, to_emails: List[str]):
        """Send email via SMTP (synchronous, called in thread)."""
        with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
            if self.smtp_use_tls:
                server.starttls()
            
            if self.smtp_user and self.smtp_password:
                server.login(self.smtp_user, self.smtp_password)
            
            server.send_message(msg, to_addrs=to_emails)
    
    async def send_rate_limit_alert(
        self,
        to_emails: List[str],
        api_key_id: Optional[str],
        client_ip: str,
        limit: int,
        window: str,
        path: str
    ) -> bool:
        """Send rate limit exceeded alert."""
        subject = f"⚠️ Rate Limit Exceeded - AgentAnywhere Universal Gateway"
        
        body = f"""
Rate limit exceeded on AgentAnywhere Universal Gateway

Details:
- API Key ID: {api_key_id or 'N/A'}
- Client IP: {client_ip}
- Limit: {limit} requests per {window}
- Path: {path}
- Timestamp: {utcnow().isoformat()}

Please review your API usage or contact support to increase your rate limits.
"""
        
        html_body = f"""
<html>
<body>
<h2>⚠️ Rate Limit Exceeded</h2>
<p>Rate limit exceeded on AgentAnywhere Universal Gateway</p>
<table border="1" cellpadding="5" cellspacing="0">
<tr><td><strong>API Key ID:</strong></td><td>{api_key_id or 'N/A'}</td></tr>
<tr><td><strong>Client IP:</strong></td><td>{client_ip}</td></tr>
<tr><td><strong>Limit:</strong></td><td>{limit} requests per {window}</td></tr>
<tr><td><strong>Path:</strong></td><td>{path}</td></tr>
<tr><td><strong>Timestamp:</strong></td><td>{utcnow().isoformat()}</td></tr>
</table>
<p>Please review your API usage or contact support to increase your rate limits.</p>
</body>
</html>
"""
        
        return await self.send_email(to_emails, subject, body, html_body)
    
    async def send_circuit_breaker_alert(
        self,
        to_emails: List[str],
        backend_url: str,
        state: str,
        previous_state: str,
        failure_count: int,
        message: str
    ) -> bool:
        """Send circuit breaker state change alert."""
        subject = f"🔴 Circuit Breaker {state.upper()} - AgentAnywhere Universal Gateway"
        
        body = f"""
Circuit breaker state changed on AgentAnywhere Universal Gateway

Details:
- Backend URL: {backend_url}
- Current State: {state.upper()}
- Previous State: {previous_state.upper()}
- Failure Count: {failure_count}
- Message: {message}
- Timestamp: {utcnow().isoformat()}

Please investigate the backend service health.
"""
        
        html_body = f"""
<html>
<body>
<h2>🔴 Circuit Breaker {state.upper()}</h2>
<p>Circuit breaker state changed on AgentAnywhere Universal Gateway</p>
<table border="1" cellpadding="5" cellspacing="0">
<tr><td><strong>Backend URL:</strong></td><td>{backend_url}</td></tr>
<tr><td><strong>Current State:</strong></td><td>{state.upper()}</td></tr>
<tr><td><strong>Previous State:</strong></td><td>{previous_state.upper()}</td></tr>
<tr><td><strong>Failure Count:</strong></td><td>{failure_count}</td></tr>
<tr><td><strong>Message:</strong></td><td>{message}</td></tr>
<tr><td><strong>Timestamp:</strong></td><td>{utcnow().isoformat()}</td></tr>
</table>
<p>Please investigate the backend service health.</p>
</body>
</html>
"""
        
        return await self.send_email(to_emails, subject, body, html_body)
    
    async def send_anomaly_alert(
        self,
        to_emails: List[str],
        client_id: str,
        endpoint: str,
        anomaly_score: float,
        request_id: Optional[str],
        method: Optional[str],
        path: Optional[str],
        status_code: Optional[int],
        response_time_ms: Optional[int],
        client_ip: Optional[str]
    ) -> bool:
        """Send anomaly detected alert."""
        subject = f"🚨 Anomaly Detected - AgentAnywhere Universal Gateway"
        
        body = f"""
Anomaly detected on AgentAnywhere Universal Gateway

Details:
- Client ID: {client_id}
- Endpoint: {endpoint}
- Anomaly Score: {anomaly_score:.2f}
- Request ID: {request_id or 'N/A'}
- Method: {method or 'N/A'}
- Path: {path or 'N/A'}
- Status Code: {status_code or 'N/A'}
- Response Time: {response_time_ms or 'N/A'} ms
- Client IP: {client_ip or 'N/A'}
- Timestamp: {utcnow().isoformat()}

Please review this request for potential security issues or unusual behavior.
"""
        
        html_body = f"""
<html>
<body>
<h2>🚨 Anomaly Detected</h2>
<p>Anomaly detected on AgentAnywhere Universal Gateway</p>
<table border="1" cellpadding="5" cellspacing="0">
<tr><td><strong>Client ID:</strong></td><td>{client_id}</td></tr>
<tr><td><strong>Endpoint:</strong></td><td>{endpoint}</td></tr>
<tr><td><strong>Anomaly Score:</strong></td><td>{anomaly_score:.2f}</td></tr>
<tr><td><strong>Request ID:</strong></td><td>{request_id or 'N/A'}</td></tr>
<tr><td><strong>Method:</strong></td><td>{method or 'N/A'}</td></tr>
<tr><td><strong>Path:</strong></td><td>{path or 'N/A'}</td></tr>
<tr><td><strong>Status Code:</strong></td><td>{status_code or 'N/A'}</td></tr>
<tr><td><strong>Response Time:</strong></td><td>{response_time_ms or 'N/A'} ms</td></tr>
<tr><td><strong>Client IP:</strong></td><td>{client_ip or 'N/A'}</td></tr>
<tr><td><strong>Timestamp:</strong></td><td>{utcnow().isoformat()}</td></tr>
</table>
<p>Please review this request for potential security issues or unusual behavior.</p>
</body>
</html>
"""
        
        return await self.send_email(to_emails, subject, body, html_body)
    
    async def send_api_key_alert(
        self,
        to_emails: List[str],
        event_type: str,
        api_key_id: str,
        api_key_name: Optional[str],
        created_by: Optional[str]
    ) -> bool:
        """Send API key created/revoked alert."""
        subject = f"🔑 API Key {event_type.upper()} - AgentAnywhere Universal Gateway"
        
        body = f"""
API key {event_type} on AgentAnywhere Universal Gateway

Details:
- API Key ID: {api_key_id}
- API Key Name: {api_key_name or 'N/A'}
- Created By: {created_by or 'N/A'}
- Event: {event_type.upper()}
- Timestamp: {utcnow().isoformat()}

This is an informational notification about API key management.
"""
        
        html_body = f"""
<html>
<body>
<h2>🔑 API Key {event_type.upper()}</h2>
<p>API key {event_type} on AgentAnywhere Universal Gateway</p>
<table border="1" cellpadding="5" cellspacing="0">
<tr><td><strong>API Key ID:</strong></td><td>{api_key_id}</td></tr>
<tr><td><strong>API Key Name:</strong></td><td>{api_key_name or 'N/A'}</td></tr>
<tr><td><strong>Created By:</strong></td><td>{created_by or 'N/A'}</td></tr>
<tr><td><strong>Event:</strong></td><td>{event_type.upper()}</td></tr>
<tr><td><strong>Timestamp:</strong></td><td>{utcnow().isoformat()}</td></tr>
</table>
<p>This is an informational notification about API key management.</p>
</body>
</html>
"""
        
        return await self.send_email(to_emails, subject, body, html_body)
    
    async def send_error_rate_alert(
        self,
        to_emails: List[str],
        error_rate: float,
        total_requests: int,
        error_count: int,
        time_window: str
    ) -> bool:
        """Send high error rate alert."""
        subject = f"⚠️ High Error Rate Detected - AgentAnywhere Universal Gateway"
        
        body = f"""
High error rate detected on AgentAnywhere Universal Gateway

Details:
- Error Rate: {error_rate:.1%}
- Total Requests: {total_requests}
- Error Count: {error_count}
- Time Window: {time_window}
- Timestamp: {utcnow().isoformat()}

Please investigate the cause of the increased error rate.
"""
        
        html_body = f"""
<html>
<body>
<h2>⚠️ High Error Rate Detected</h2>
<p>High error rate detected on AgentAnywhere Universal Gateway</p>
<table border="1" cellpadding="5" cellspacing="0">
<tr><td><strong>Error Rate:</strong></td><td>{error_rate:.1%}</td></tr>
<tr><td><strong>Total Requests:</strong></td><td>{total_requests}</td></tr>
<tr><td><strong>Error Count:</strong></td><td>{error_count}</td></tr>
<tr><td><strong>Time Window:</strong></td><td>{time_window}</td></tr>
<tr><td><strong>Timestamp:</strong></td><td>{utcnow().isoformat()}</td></tr>
</table>
<p>Please investigate the cause of the increased error rate.</p>
</body>
</html>
"""
        
        return await self.send_email(to_emails, subject, body, html_body)
    
    async def send_traffic_spike_alert(
        self,
        to_emails: List[str],
        current_rpm: float,
        baseline_rpm: float,
        multiplier: float,
        time_window: str
    ) -> bool:
        """Send traffic spike alert."""
        subject = f"📈 Traffic Spike Detected - AgentAnywhere Universal Gateway"
        
        body = f"""
Traffic spike detected on AgentAnywhere Universal Gateway

Details:
- Current RPM: {current_rpm:.0f}
- Baseline RPM: {baseline_rpm:.0f}
- Multiplier: {multiplier:.1f}x
- Time Window: {time_window}
- Timestamp: {utcnow().isoformat()}

This may indicate increased load or a potential DDoS attack.
"""
        
        html_body = f"""
<html>
<body>
<h2>📈 Traffic Spike Detected</h2>
<p>Traffic spike detected on AgentAnywhere Universal Gateway</p>
<table border="1" cellpadding="5" cellspacing="0">
<tr><td><strong>Current RPM:</strong></td><td>{current_rpm:.0f}</td></tr>
<tr><td><strong>Baseline RPM:</strong></td><td>{baseline_rpm:.0f}</td></tr>
<tr><td><strong>Multiplier:</strong></td><td>{multiplier:.1f}x</td></tr>
<tr><td><strong>Time Window:</strong></td><td>{time_window}</td></tr>
<tr><td><strong>Timestamp:</strong></td><td>{utcnow().isoformat()}</td></tr>
</table>
<p>This may indicate increased load or a potential DDoS attack.</p>
</body>
</html>
"""
        
        return await self.send_email(to_emails, subject, body, html_body)
    
    async def send_request_error_alert(
        self,
        to_emails: List[str],
        error_type: str,
        path: str,
        method: str,
        status_code: Optional[int],
        error_message: str,
        request_id: Optional[str],
        client_ip: Optional[str]
    ) -> bool:
        """Send request error alert."""
        subject = f"❌ Request Error - AgentAnywhere Universal Gateway"
        
        body = f"""
Request error on AgentAnywhere Universal Gateway

Details:
- Error Type: {error_type}
- Path: {path}
- Method: {method}
- Status Code: {status_code or 'N/A'}
- Error Message: {error_message}
- Request ID: {request_id or 'N/A'}
- Client IP: {client_ip or 'N/A'}
- Timestamp: {utcnow().isoformat()}

Please investigate this error.
"""
        
        html_body = f"""
<html>
<body>
<h2>❌ Request Error</h2>
<p>Request error on AgentAnywhere Universal Gateway</p>
<table border="1" cellpadding="5" cellspacing="0">
<tr><td><strong>Error Type:</strong></td><td>{error_type}</td></tr>
<tr><td><strong>Path:</strong></td><td>{path}</td></tr>
<tr><td><strong>Method:</strong></td><td>{method}</td></tr>
<tr><td><strong>Status Code:</strong></td><td>{status_code or 'N/A'}</td></tr>
<tr><td><strong>Error Message:</strong></td><td>{error_message}</td></tr>
<tr><td><strong>Request ID:</strong></td><td>{request_id or 'N/A'}</td></tr>
<tr><td><strong>Client IP:</strong></td><td>{client_ip or 'N/A'}</td></tr>
<tr><td><strong>Timestamp:</strong></td><td>{utcnow().isoformat()}</td></tr>
</table>
<p>Please investigate this error.</p>
</body>
</html>
"""
        
        return await self.send_email(to_emails, subject, body, html_body)
    
    async def test_connection(self) -> bool:
        """Test SMTP connection."""
        if not self.enabled:
            return False
        
        try:
            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                if self.smtp_use_tls:
                    server.starttls()
                if self.smtp_user and self.smtp_password:
                    server.login(self.smtp_user, self.smtp_password)
            return True
        except Exception as e:
            logger.error("email_test_failed", error=str(e))
            return False


class SlackNotificationService:
    """Slack notification service using Incoming Webhooks.

    Slack webhooks are very simple: POST a JSON body to the webhook URL. We
    format alerts as Slack ``blocks`` for nicer rendering and fall back to
    plain ``text`` if blocks are unsupported.
    """

    def __init__(
        self,
        webhook_url: str,
        default_channel: Optional[str] = None,
        username: str = "AgentAnywhere Gateway",
        timeout: float = 10.0,
    ) -> None:
        self.webhook_url = webhook_url
        self.default_channel = default_channel
        self.username = username
        self.timeout = timeout
        self.enabled = bool(webhook_url)
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def send_message(
        self,
        text: str,
        title: Optional[str] = None,
        fields: Optional[Dict[str, Any]] = None,
        color: str = "#2eb67d",
        channel: Optional[str] = None,
    ) -> bool:
        """Post a message to Slack. Returns ``True`` on HTTP 2xx."""
        if not self.enabled:
            logger.warning("slack_notification_disabled")
            return False

        blocks: List[Dict[str, Any]] = []
        if title:
            blocks.append(
                {
                    "type": "header",
                    "text": {"type": "plain_text", "text": title[:150]},
                }
            )
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": text[:3000]}}
        )
        if fields:
            field_blocks = []
            for key, value in fields.items():
                field_blocks.append(
                    {"type": "mrkdwn", "text": f"*{key}*\n{value}"}
                )
                if len(field_blocks) == 10:
                    break
            if field_blocks:
                blocks.append({"type": "section", "fields": field_blocks})

        payload: Dict[str, Any] = {
            "username": self.username,
            "text": title or text,
            "attachments": [
                {
                    "color": color,
                    "blocks": blocks,
                }
            ],
        }
        if channel or self.default_channel:
            payload["channel"] = channel or self.default_channel

        try:
            response = await self.client.post(self.webhook_url, json=payload)
            ok = 200 <= response.status_code < 300
            if not ok:
                logger.warning(
                    "slack_send_failed",
                    status_code=response.status_code,
                    body=response.text[:200],
                )
            return ok
        except Exception as e:
            logger.error("slack_send_error", error=str(e))
            return False

    async def test_connection(self) -> bool:
        """Verify Slack webhook reachability with a no-op ping."""
        if not self.enabled:
            return False
        return await self.send_message(
            text=":white_check_mark: AgentAnywhere Universal Gateway Slack connection test",
            title="Connection Test",
            color="#36a64f",
        )

    # ---- alert helpers (mirror EmailNotificationService) --------------------
    async def send_rate_limit_alert(
        self,
        api_key_id: Optional[str],
        client_ip: str,
        limit: int,
        window: str,
        path: str,
    ) -> bool:
        return await self.send_message(
            title="Rate Limit Exceeded",
            text=f"Rate limit exceeded for `{path}`",
            fields={
                "API Key ID": api_key_id or "N/A",
                "Client IP": client_ip,
                "Limit": f"{limit} per {window}",
                "Path": path,
                "Timestamp": utcnow().isoformat(),
            },
            color="#ECB22E",
        )

    async def send_circuit_breaker_alert(
        self,
        backend_url: str,
        state: str,
        previous_state: str,
        failure_count: int,
        message: str,
    ) -> bool:
        return await self.send_message(
            title=f"Circuit Breaker {state.upper()}",
            text=message,
            fields={
                "Backend URL": backend_url,
                "Previous State": previous_state,
                "Failure Count": failure_count,
                "Timestamp": utcnow().isoformat(),
            },
            color="#E01E5A",
        )

    async def send_anomaly_alert(
        self,
        client_id: str,
        endpoint: str,
        anomaly_score: float,
        request_id: Optional[str],
        method: Optional[str],
        path: Optional[str],
        status_code: Optional[int],
        response_time_ms: Optional[int],
        client_ip: Optional[str],
    ) -> bool:
        return await self.send_message(
            title="Anomaly Detected",
            text=f"Anomaly score `{anomaly_score:.2f}` for client `{client_id}`",
            fields={
                "Endpoint": endpoint,
                "Request ID": request_id or "N/A",
                "Method": method or "N/A",
                "Path": path or "N/A",
                "Status Code": status_code if status_code is not None else "N/A",
                "Response Time": f"{response_time_ms} ms" if response_time_ms is not None else "N/A",
                "Client IP": client_ip or "N/A",
            },
            color="#E01E5A",
        )

    async def send_api_key_alert(
        self,
        event_type: str,
        api_key_id: str,
        api_key_name: Optional[str],
        created_by: Optional[str],
    ) -> bool:
        return await self.send_message(
            title=f"API Key {event_type.upper()}",
            text=f"API key {event_type}",
            fields={
                "API Key ID": api_key_id,
                "Name": api_key_name or "N/A",
                "Actor": created_by or "N/A",
                "Timestamp": utcnow().isoformat(),
            },
            color="#36C5F0",
        )

    async def send_error_rate_alert(
        self,
        error_rate: float,
        total_requests: int,
        error_count: int,
        time_window: str,
    ) -> bool:
        return await self.send_message(
            title="High Error Rate",
            text=f"Error rate `{error_rate:.1%}` over {time_window}",
            fields={
                "Total Requests": total_requests,
                "Errors": error_count,
                "Window": time_window,
            },
            color="#ECB22E",
        )

    async def send_traffic_spike_alert(
        self,
        current_rpm: float,
        baseline_rpm: float,
        multiplier: float,
        time_window: str,
    ) -> bool:
        return await self.send_message(
            title="Traffic Spike",
            text=f"Traffic is `{multiplier:.1f}x` baseline",
            fields={
                "Current RPM": f"{current_rpm:.0f}",
                "Baseline RPM": f"{baseline_rpm:.0f}",
                "Window": time_window,
            },
            color="#36C5F0",
        )

    async def send_request_error_alert(
        self,
        error_type: str,
        path: str,
        method: str,
        status_code: Optional[int],
        error_message: str,
        request_id: Optional[str],
        client_ip: Optional[str],
    ) -> bool:
        return await self.send_message(
            title=f"Request Error: {error_type}",
            text=f"`{method}` {path} failed",
            fields={
                "Status Code": status_code if status_code is not None else "N/A",
                "Error": error_message,
                "Request ID": request_id or "N/A",
                "Client IP": client_ip or "N/A",
            },
            color="#E01E5A",
        )


class AlertThresholds:
    """Manages alert thresholds configuration."""
    
    def __init__(self):
        self.rate_limit_enabled: bool = True
        self.circuit_breaker_enabled: bool = True
        self.anomaly_score_threshold: float = 0.7
        self.error_rate_threshold: float = 0.1
        self.traffic_spike_threshold: float = 2.0
        self.min_interval_seconds: int = 300
        
        # Load from settings
        self._load_from_settings()
    
    def _load_from_settings(self):
        """Load thresholds from settings."""
        self.rate_limit_enabled = settings.alert_threshold_rate_limit
        self.circuit_breaker_enabled = settings.alert_threshold_circuit_breaker
        self.anomaly_score_threshold = settings.alert_threshold_anomaly_score
        self.error_rate_threshold = settings.alert_threshold_error_rate
        self.traffic_spike_threshold = settings.alert_threshold_traffic_spike
        self.min_interval_seconds = settings.alert_threshold_min_interval_seconds
    
    def update(
        self,
        rate_limit_enabled: Optional[bool] = None,
        circuit_breaker_enabled: Optional[bool] = None,
        anomaly_score_threshold: Optional[float] = None,
        error_rate_threshold: Optional[float] = None,
        traffic_spike_threshold: Optional[float] = None,
        min_interval_seconds: Optional[int] = None
    ):
        """Update alert thresholds."""
        if rate_limit_enabled is not None:
            self.rate_limit_enabled = rate_limit_enabled
        if circuit_breaker_enabled is not None:
            self.circuit_breaker_enabled = circuit_breaker_enabled
        if anomaly_score_threshold is not None:
            self.anomaly_score_threshold = anomaly_score_threshold
        if error_rate_threshold is not None:
            self.error_rate_threshold = error_rate_threshold
        if traffic_spike_threshold is not None:
            self.traffic_spike_threshold = traffic_spike_threshold
        if min_interval_seconds is not None:
            self.min_interval_seconds = min_interval_seconds
        
        logger.info("alert_thresholds_updated", thresholds=self.to_dict())
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert thresholds to dictionary."""
        return {
            "rate_limit_enabled": self.rate_limit_enabled,
            "circuit_breaker_enabled": self.circuit_breaker_enabled,
            "anomaly_score_threshold": self.anomaly_score_threshold,
            "error_rate_threshold": self.error_rate_threshold,
            "traffic_spike_threshold": self.traffic_spike_threshold,
            "min_interval_seconds": self.min_interval_seconds,
        }
    
    def should_alert_anomaly(self, anomaly_score: float) -> bool:
        """Check if anomaly score exceeds threshold."""
        return self.anomaly_score_threshold > 0 and anomaly_score >= self.anomaly_score_threshold
    
    def should_alert_error_rate(self, error_rate: float) -> bool:
        """Check if error rate exceeds threshold."""
        return self.error_rate_threshold > 0 and error_rate >= self.error_rate_threshold
    
    def should_alert_traffic_spike(self, multiplier: float) -> bool:
        """Check if traffic spike exceeds threshold."""
        return self.traffic_spike_threshold > 0 and multiplier >= self.traffic_spike_threshold


class NotificationManager:
    """Manages all notification channels."""
    
    def __init__(self):
        self.email_service: Optional[EmailNotificationService] = None
        self.slack_service: Optional[SlackNotificationService] = None
        self.alert_recipients: List[str] = []
        self.alert_thresholds = AlertThresholds()
        
        # Rate limiting for emails (prevent spam)
        self.email_sent_times: List[float] = []
        self.email_rate_limit_per_hour: int = 50

        # Rate limiting for Slack
        self.slack_sent_times: List[float] = []
        self.slack_rate_limit_per_hour: int = 100

        # Alert deduplication (prevent duplicate alerts within min_interval)
        self.last_alert_times: Dict[str, float] = defaultdict(float)
    
    def configure_email(
        self,
        smtp_host: str,
        smtp_port: int = 587,
        smtp_user: Optional[str] = None,
        smtp_password: Optional[str] = None,
        smtp_use_tls: bool = True,
        from_email: Optional[str] = None
    ):
        """Configure email notification service."""
        self.email_service = EmailNotificationService(
            smtp_host=smtp_host,
            smtp_port=smtp_port,
            smtp_user=smtp_user,
            smtp_password=smtp_password,
            smtp_use_tls=smtp_use_tls,
            from_email=from_email
        )
        self.email_rate_limit_per_hour = settings.email_rate_limit_per_hour
        logger.info("email_notification_configured", smtp_host=smtp_host)

    def configure_slack(
        self,
        webhook_url: str,
        default_channel: Optional[str] = None,
        username: str = "AgentAnywhere Gateway",
    ) -> None:
        """Configure Slack notification service."""
        self.slack_service = SlackNotificationService(
            webhook_url=webhook_url,
            default_channel=default_channel,
            username=username,
        )
        try:
            self.slack_rate_limit_per_hour = getattr(
                settings, "slack_rate_limit_per_hour", self.slack_rate_limit_per_hour
            )
        except Exception:
            pass
        logger.info("slack_notification_configured", default_channel=default_channel)
    
    def set_alert_recipients(self, emails: List[str]):
        """Set email recipients for alerts."""
        self.alert_recipients = emails
        logger.info("alert_recipients_set", count=len(emails))
    
    def set_alert_recipients_from_string(self, emails_str: str):
        """Set email recipients from comma-separated string."""
        if emails_str:
            self.alert_recipients = [email.strip() for email in emails_str.split(",") if email.strip()]
            logger.info("alert_recipients_set", count=len(self.alert_recipients))
    
    def _check_email_rate_limit(self) -> bool:
        """Check if email rate limit is not exceeded."""
        now = time.time()
        # Remove emails older than 1 hour
        self.email_sent_times = [t for t in self.email_sent_times if now - t < 3600]
        
        if len(self.email_sent_times) >= self.email_rate_limit_per_hour:
            logger.warning("email_rate_limit_exceeded", count=len(self.email_sent_times))
            return False
        
        return True
    
    def _check_alert_interval(self, alert_key: str) -> bool:
        """Check if enough time has passed since last alert of this type."""
        now = time.time()
        last_time = self.last_alert_times.get(alert_key, 0)
        
        if now - last_time < self.alert_thresholds.min_interval_seconds:
            logger.debug("alert_suppressed_interval", alert_key=alert_key, seconds_remaining=int(self.alert_thresholds.min_interval_seconds - (now - last_time)))
            return False
        
        self.last_alert_times[alert_key] = now
        return True
    
    def _record_email_sent(self):
        """Record that an email was sent."""
        self.email_sent_times.append(time.time())

    def _check_slack_rate_limit(self) -> bool:
        """Check whether we're under the Slack hourly send budget."""
        now = time.time()
        self.slack_sent_times = [t for t in self.slack_sent_times if now - t < 3600]
        if len(self.slack_sent_times) >= self.slack_rate_limit_per_hour:
            logger.warning("slack_rate_limit_exceeded", count=len(self.slack_sent_times))
            return False
        return True

    def _record_slack_sent(self) -> None:
        self.slack_sent_times.append(time.time())

    async def _dispatch(self, channel_name: str, coro_factory) -> None:
        """Helper to invoke a service alert + record send time on success."""
        try:
            success = await coro_factory()
            if success:
                if channel_name == "email":
                    self._record_email_sent()
                elif channel_name == "slack":
                    self._record_slack_sent()
        except Exception as e:
            logger.warning(
                "notification_dispatch_failed", channel=channel_name, error=str(e)
            )
    
    async def send_rate_limit_alert(
        self,
        api_key_id: Optional[str],
        client_ip: str,
        limit: int,
        window: str,
        path: str
    ):
        """Send rate limit alert via all configured channels."""
        if not self.alert_thresholds.rate_limit_enabled:
            return

        alert_key = f"rate_limit:{api_key_id or client_ip}:{path}"
        if not self._check_alert_interval(alert_key):
            return

        if self.email_service and self.alert_recipients and self._check_email_rate_limit():
            await self._dispatch(
                "email",
                lambda: self.email_service.send_rate_limit_alert(
                    self.alert_recipients,
                    api_key_id,
                    client_ip,
                    limit,
                    window,
                    path,
                ),
            )

        if self.slack_service and self._check_slack_rate_limit():
            await self._dispatch(
                "slack",
                lambda: self.slack_service.send_rate_limit_alert(
                    api_key_id, client_ip, limit, window, path
                ),
            )
    
    async def send_circuit_breaker_alert(
        self,
        backend_url: str,
        state: str,
        previous_state: str,
        failure_count: int,
        message: str
    ):
        """Send circuit breaker alert."""
        if not self.alert_thresholds.circuit_breaker_enabled:
            return

        alert_key = f"circuit_breaker:{backend_url}:{state}"
        if not self._check_alert_interval(alert_key):
            return

        if self.email_service and self.alert_recipients and self._check_email_rate_limit():
            await self._dispatch(
                "email",
                lambda: self.email_service.send_circuit_breaker_alert(
                    self.alert_recipients,
                    backend_url,
                    state,
                    previous_state,
                    failure_count,
                    message,
                ),
            )

        if self.slack_service and self._check_slack_rate_limit():
            await self._dispatch(
                "slack",
                lambda: self.slack_service.send_circuit_breaker_alert(
                    backend_url, state, previous_state, failure_count, message
                ),
            )
    
    async def send_anomaly_alert(
        self,
        client_id: str,
        endpoint: str,
        anomaly_score: float,
        request_id: Optional[str] = None,
        method: Optional[str] = None,
        path: Optional[str] = None,
        status_code: Optional[int] = None,
        response_time_ms: Optional[int] = None,
        client_ip: Optional[str] = None
    ):
        """Send anomaly alert."""
        if not self.alert_thresholds.should_alert_anomaly(anomaly_score):
            return

        alert_key = f"anomaly:{client_id}:{endpoint}"
        if not self._check_alert_interval(alert_key):
            return

        if self.email_service and self.alert_recipients and self._check_email_rate_limit():
            await self._dispatch(
                "email",
                lambda: self.email_service.send_anomaly_alert(
                    self.alert_recipients,
                    client_id,
                    endpoint,
                    anomaly_score,
                    request_id,
                    method,
                    path,
                    status_code,
                    response_time_ms,
                    client_ip,
                ),
            )

        if self.slack_service and self._check_slack_rate_limit():
            await self._dispatch(
                "slack",
                lambda: self.slack_service.send_anomaly_alert(
                    client_id,
                    endpoint,
                    anomaly_score,
                    request_id,
                    method,
                    path,
                    status_code,
                    response_time_ms,
                    client_ip,
                ),
            )
    
    async def send_api_key_alert(
        self,
        event_type: str,
        api_key_id: str,
        api_key_name: Optional[str] = None,
        created_by: Optional[str] = None
    ):
        """Send API key created/revoked alert."""
        alert_key = f"api_key:{event_type}:{api_key_id}"
        if not self._check_alert_interval(alert_key):
            return

        if self.email_service and self.alert_recipients and self._check_email_rate_limit():
            await self._dispatch(
                "email",
                lambda: self.email_service.send_api_key_alert(
                    self.alert_recipients,
                    event_type,
                    api_key_id,
                    api_key_name,
                    created_by,
                ),
            )

        if self.slack_service and self._check_slack_rate_limit():
            await self._dispatch(
                "slack",
                lambda: self.slack_service.send_api_key_alert(
                    event_type, api_key_id, api_key_name, created_by
                ),
            )
    
    async def send_error_rate_alert(
        self,
        error_rate: float,
        total_requests: int,
        error_count: int,
        time_window: str = "5 minutes"
    ):
        """Send high error rate alert."""
        if not self.alert_thresholds.should_alert_error_rate(error_rate):
            return

        alert_key = "error_rate:global"
        if not self._check_alert_interval(alert_key):
            return

        if self.email_service and self.alert_recipients and self._check_email_rate_limit():
            await self._dispatch(
                "email",
                lambda: self.email_service.send_error_rate_alert(
                    self.alert_recipients,
                    error_rate,
                    total_requests,
                    error_count,
                    time_window,
                ),
            )

        if self.slack_service and self._check_slack_rate_limit():
            await self._dispatch(
                "slack",
                lambda: self.slack_service.send_error_rate_alert(
                    error_rate, total_requests, error_count, time_window
                ),
            )
    
    async def send_traffic_spike_alert(
        self,
        current_rpm: float,
        baseline_rpm: float,
        multiplier: float,
        time_window: str = "5 minutes"
    ):
        """Send traffic spike alert."""
        if not self.alert_thresholds.should_alert_traffic_spike(multiplier):
            return

        alert_key = "traffic_spike:global"
        if not self._check_alert_interval(alert_key):
            return

        if self.email_service and self.alert_recipients and self._check_email_rate_limit():
            await self._dispatch(
                "email",
                lambda: self.email_service.send_traffic_spike_alert(
                    self.alert_recipients,
                    current_rpm,
                    baseline_rpm,
                    multiplier,
                    time_window,
                ),
            )

        if self.slack_service and self._check_slack_rate_limit():
            await self._dispatch(
                "slack",
                lambda: self.slack_service.send_traffic_spike_alert(
                    current_rpm, baseline_rpm, multiplier, time_window
                ),
            )
    
    async def send_request_error_alert(
        self,
        error_type: str,
        path: str,
        method: str,
        status_code: Optional[int],
        error_message: str,
        request_id: Optional[str] = None,
        client_ip: Optional[str] = None
    ):
        """Send request error alert."""
        alert_key = f"request_error:{error_type}:{path}"
        if not self._check_alert_interval(alert_key):
            return

        if self.email_service and self.alert_recipients and self._check_email_rate_limit():
            await self._dispatch(
                "email",
                lambda: self.email_service.send_request_error_alert(
                    self.alert_recipients,
                    error_type,
                    path,
                    method,
                    status_code,
                    error_message,
                    request_id,
                    client_ip,
                ),
            )

        if self.slack_service and self._check_slack_rate_limit():
            await self._dispatch(
                "slack",
                lambda: self.slack_service.send_request_error_alert(
                    error_type,
                    path,
                    method,
                    status_code,
                    error_message,
                    request_id,
                    client_ip,
                ),
            )
    
    def get_alert_thresholds(self) -> Dict[str, Any]:
        """Get current alert thresholds."""
        return self.alert_thresholds.to_dict()
    
    def update_alert_thresholds(
        self,
        rate_limit_enabled: Optional[bool] = None,
        circuit_breaker_enabled: Optional[bool] = None,
        anomaly_score_threshold: Optional[float] = None,
        error_rate_threshold: Optional[float] = None,
        traffic_spike_threshold: Optional[float] = None,
        min_interval_seconds: Optional[int] = None
    ):
        """Update alert thresholds."""
        self.alert_thresholds.update(
            rate_limit_enabled=rate_limit_enabled,
            circuit_breaker_enabled=circuit_breaker_enabled,
            anomaly_score_threshold=anomaly_score_threshold,
            error_rate_threshold=error_rate_threshold,
            traffic_spike_threshold=traffic_spike_threshold,
            min_interval_seconds=min_interval_seconds
        )


# Global notification manager
notification_manager = NotificationManager()
