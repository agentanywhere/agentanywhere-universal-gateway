"""Tests for the Slack notification channel and its integration with NotificationManager."""
import time
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from app.notifications import (
    EmailNotificationService,
    NotificationManager,
    SlackNotificationService,
)


@pytest.fixture
def slack_service():
    """A SlackNotificationService bound to a fake webhook URL."""
    return SlackNotificationService(
        webhook_url="https://hooks.slack.com/services/T000/B000/XXXX",
        default_channel="#alerts",
        username="AgentAnywhere Gateway",
    )


@pytest.fixture
def manager_with_slack(slack_service):
    """A NotificationManager with mocked email + a real (but stubbed) Slack service."""
    manager = NotificationManager()

    # Email side stubbed so we can assert dispatch happens to both channels.
    manager.email_service = Mock(spec=EmailNotificationService)
    manager.email_service.enabled = True
    for method in (
        "send_rate_limit_alert",
        "send_circuit_breaker_alert",
        "send_anomaly_alert",
        "send_api_key_alert",
        "send_error_rate_alert",
        "send_traffic_spike_alert",
        "send_request_error_alert",
    ):
        setattr(manager.email_service, method, AsyncMock(return_value=True))
    manager.alert_recipients = ["ops@example.com"]

    # Replace Slack client with a stub that always returns a 200.
    slack_service.send_message = AsyncMock(return_value=True)
    manager.slack_service = slack_service
    return manager


class TestSlackNotificationService:
    def test_init_disabled_when_webhook_missing(self):
        s = SlackNotificationService(webhook_url="")
        assert s.enabled is False

    @pytest.mark.asyncio
    async def test_send_message_disabled_returns_false(self):
        s = SlackNotificationService(webhook_url="")
        assert await s.send_message("hello") is False

    @pytest.mark.asyncio
    async def test_send_message_posts_payload(self, slack_service):
        captured = {}

        async def fake_post(url, json, **kw):
            captured["url"] = url
            captured["json"] = json
            request = httpx.Request("POST", url)
            return httpx.Response(200, request=request)

        slack_service._client = AsyncMock()
        slack_service._client.post = fake_post

        ok = await slack_service.send_message(
            text="something happened",
            title="Test Alert",
            fields={"Key": "value"},
            color="#36a64f",
        )

        assert ok is True
        assert captured["url"].startswith("https://hooks.slack.com/")
        body = captured["json"]
        assert body["channel"] == "#alerts"
        assert body["username"] == "AgentAnywhere Gateway"
        assert body["text"] == "Test Alert"
        attachments = body["attachments"]
        assert len(attachments) == 1
        assert attachments[0]["color"] == "#36a64f"
        # Check that the title block is present.
        block_types = [b["type"] for b in attachments[0]["blocks"]]
        assert "header" in block_types
        assert "section" in block_types

    @pytest.mark.asyncio
    async def test_send_message_handles_non_2xx(self, slack_service):
        async def fake_post(url, json, **kw):
            request = httpx.Request("POST", url)
            return httpx.Response(500, text="oops", request=request)

        slack_service._client = AsyncMock()
        slack_service._client.post = fake_post
        assert await slack_service.send_message("hello") is False

    @pytest.mark.asyncio
    async def test_test_connection_pings(self, slack_service):
        slack_service.send_message = AsyncMock(return_value=True)
        assert await slack_service.test_connection() is True
        slack_service.send_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_alert_helpers_call_send_message(self, slack_service):
        slack_service.send_message = AsyncMock(return_value=True)

        await slack_service.send_rate_limit_alert(
            "key-1", "1.2.3.4", 60, "minute", "/api/x"
        )
        await slack_service.send_circuit_breaker_alert(
            "http://b", "open", "closed", 5, "tripped"
        )
        await slack_service.send_anomaly_alert(
            "client-1", "/x", 0.9, "req-1", "GET", "/x", 500, 200, "1.2.3.4"
        )
        await slack_service.send_api_key_alert("created", "k1", "name", "actor")
        await slack_service.send_error_rate_alert(0.2, 100, 20, "5m")
        await slack_service.send_traffic_spike_alert(200.0, 100.0, 2.0, "5m")
        await slack_service.send_request_error_alert(
            "TimeoutError", "/x", "GET", 504, "timed out", "req-1", "1.2.3.4"
        )

        assert slack_service.send_message.await_count == 7


class TestNotificationManagerSlack:
    @pytest.mark.asyncio
    async def test_dispatches_to_both_channels(self, manager_with_slack):
        await manager_with_slack.send_rate_limit_alert(
            "k1", "1.2.3.4", 60, "minute", "/api/x"
        )
        manager_with_slack.email_service.send_rate_limit_alert.assert_awaited_once()
        manager_with_slack.slack_service.send_message.assert_awaited()

    @pytest.mark.asyncio
    async def test_slack_rate_limit_blocks_further_sends(self, manager_with_slack):
        manager_with_slack.slack_rate_limit_per_hour = 1
        # First send goes through.
        await manager_with_slack.send_circuit_breaker_alert(
            "http://b", "open", "closed", 5, "tripped"
        )
        first_count = manager_with_slack.slack_service.send_message.await_count

        # Reset interval dedup for the same alert key by changing state.
        manager_with_slack.last_alert_times.clear()

        # Second send: Slack budget already consumed, so it must skip.
        await manager_with_slack.send_circuit_breaker_alert(
            "http://b", "open", "closed", 5, "tripped"
        )
        second_count = manager_with_slack.slack_service.send_message.await_count

        assert second_count == first_count, "Slack rate limit should suppress send"

    @pytest.mark.asyncio
    async def test_slack_alone_when_email_unconfigured(self, slack_service):
        manager = NotificationManager()
        manager.email_service = None
        slack_service.send_message = AsyncMock(return_value=True)
        manager.slack_service = slack_service

        await manager.send_anomaly_alert(
            client_id="c1",
            endpoint="/x",
            anomaly_score=0.99,
        )
        slack_service.send_message.assert_awaited()

    @pytest.mark.asyncio
    async def test_alert_interval_suppresses_both_channels(self, manager_with_slack):
        # First call should go through.
        await manager_with_slack.send_api_key_alert("created", "k1", "name", "actor")
        first_email = manager_with_slack.email_service.send_api_key_alert.await_count
        first_slack = manager_with_slack.slack_service.send_message.await_count

        # Second call within interval window: nothing should fire.
        await manager_with_slack.send_api_key_alert("created", "k1", "name", "actor")
        assert manager_with_slack.email_service.send_api_key_alert.await_count == first_email
        assert manager_with_slack.slack_service.send_message.await_count == first_slack

    def test_configure_slack_creates_service(self):
        manager = NotificationManager()
        manager.configure_slack("https://hooks.slack.com/services/T/B/X", "#ops")
        assert manager.slack_service is not None
        assert manager.slack_service.default_channel == "#ops"
