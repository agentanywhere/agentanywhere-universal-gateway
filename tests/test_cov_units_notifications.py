"""Unit tests for app.notifications (SMTP and Slack HTTP mocked)."""
import time
import types
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app import notifications as n


# ---------------- email ----------------
def _svc(**kw):
    return n.EmailNotificationService("smtp.test", smtp_user="u@x.com", smtp_password="pw", **kw)


def test_email_init_defaults():
    s = _svc()
    assert s.from_email == "u@x.com" and s.enabled
    assert not n.EmailNotificationService("").enabled


async def test_send_email_guards():
    assert await n.EmailNotificationService("").send_email(["a@b"], "s", "b") is False
    assert await _svc().send_email([], "s", "b") is False


async def test_send_email_success_builds_multipart(monkeypatch):
    s = _svc()
    sent = {}
    monkeypatch.setattr(s, "_send_smtp", lambda msg, to: sent.update(msg=msg, to=to))
    assert await s.send_email(["a@b.com", "c@d.com"], "Subj", "plain", "<b>h</b>") is True
    assert sent["msg"]["To"] == "a@b.com, c@d.com" and sent["msg"]["Subject"] == "Subj"
    assert len(sent["msg"].get_payload()) == 2
    assert await s.send_email(["a@b.com"], "S", "p") is True
    assert len(sent["msg"].get_payload()) == 1


async def test_send_email_smtp_failure(monkeypatch):
    s = _svc()

    def boom(*a):
        raise OSError("refused")

    monkeypatch.setattr(s, "_send_smtp", boom)
    assert await s.send_email(["a@b.com"], "S", "p") is False


@pytest.mark.parametrize("tls,creds", [(True, True), (False, False)])
def test_send_smtp_transport(monkeypatch, tls, creds):
    server = MagicMock()
    smtp = MagicMock()
    smtp.return_value.__enter__.return_value = server
    monkeypatch.setattr(n.smtplib, "SMTP", smtp)
    s = n.EmailNotificationService("h", 25, "u" if creds else None, "p" if creds else None, smtp_use_tls=tls)
    s._send_smtp("MSG", ["a@b"])
    smtp.assert_called_once_with("h", 25)
    assert server.starttls.called is tls
    assert server.login.called is creds
    server.send_message.assert_called_once_with("MSG", to_addrs=["a@b"])


async def test_email_test_connection(monkeypatch):
    assert await n.EmailNotificationService("").test_connection() is False
    server = MagicMock()
    smtp = MagicMock()
    smtp.return_value.__enter__.return_value = server
    monkeypatch.setattr(n.smtplib, "SMTP", smtp)
    s = _svc()
    assert await s.test_connection() is True
    server.starttls.assert_called_once()
    server.login.assert_called_once_with("u@x.com", "pw")
    smtp.side_effect = ConnectionRefusedError("no")
    assert await s.test_connection() is False


EMAIL_CALLS = [
    ("send_rate_limit_alert", ("k1", "1.1.1.1", 10, "1m", "/p")),
    ("send_circuit_breaker_alert", ("http://b", "OPEN", "CLOSED", 5, "msg")),
    ("send_anomaly_alert", ("c", "/e", 0.9, "rid", "GET", "/p", 500, 123, "1.1.1.1")),
    ("send_api_key_alert", ("created", "id1", "name", "me")),
    ("send_api_key_alert", ("revoked", "id1", None, None)),
    ("send_error_rate_alert", (0.5, 100, 50, "5 minutes")),
    ("send_traffic_spike_alert", (500.0, 100.0, 5.0, "5 minutes")),
    ("send_request_error_alert", ("TimeoutError", "/p", "GET", None, "slow", None, None)),
]


@pytest.mark.parametrize("method,args", EMAIL_CALLS)
async def test_email_alert_builders(method, args):
    s = _svc()
    s.send_email = AsyncMock(return_value=True)
    assert await getattr(s, method)(["ops@x.com"], *args) is True
    to, subject, body, html = s.send_email.call_args.args
    assert to == ["ops@x.com"] and subject and "AgentAnywhere" in subject
    assert body.strip() and "<html>" in html


# ---------------- slack ----------------
def _slack(**kw):
    s = n.SlackNotificationService("https://hooks.slack.test/x", **kw)
    s._client = MagicMock(post=AsyncMock(return_value=MagicMock(status_code=200, text="ok")),
                          aclose=AsyncMock())
    return s


async def test_slack_disabled_and_lazy_client():
    s = n.SlackNotificationService("")
    assert await s.send_message("t") is False
    assert await s.test_connection() is False
    s2 = n.SlackNotificationService("http://x", timeout=3)
    c = s2.client
    assert isinstance(c, httpx.AsyncClient) and s2.client is c
    await s2.close()
    assert s2._client is None
    await s2.close()


async def test_slack_send_message_payload():
    s = _slack(default_channel="#ops")
    fields = {f"k{i}": i for i in range(15)}
    assert await s.send_message("x" * 4000, title="T" * 200, fields=fields) is True
    payload = s._client.post.call_args.kwargs["json"]
    blocks = payload["attachments"][0]["blocks"]
    assert len(blocks[0]["text"]["text"]) == 150
    assert len(blocks[1]["text"]["text"]) == 3000
    assert len(blocks[2]["fields"]) == 10
    assert payload["channel"] == "#ops"
    await s.send_message("hi", channel="#other")
    p2 = s._client.post.call_args.kwargs["json"]
    assert p2["channel"] == "#other" and p2["text"] == "hi"
    s2 = _slack()
    await s2.send_message("hi", fields={})
    assert "channel" not in s2._client.post.call_args.kwargs["json"]


async def test_slack_failure_paths():
    s = _slack()
    s._client.post = AsyncMock(return_value=MagicMock(status_code=500, text="oops"))
    assert await s.send_message("t") is False
    s._client.post = AsyncMock(side_effect=httpx.ConnectTimeout("t"))
    assert await s.send_message("t") is False
    s._client.post = AsyncMock(return_value=MagicMock(status_code=204, text=""))
    assert await s.test_connection() is True


SLACK_CALLS = [
    ("send_rate_limit_alert", ("k", "1.1.1.1", 10, "1m", "/p")),
    ("send_circuit_breaker_alert", ("http://b", "OPEN", "CLOSED", 5, "m")),
    ("send_circuit_breaker_alert", ("http://b", "CLOSED", "OPEN", 0, "m")),
    ("send_anomaly_alert", ("c", "/e", 0.9, "r", "GET", "/p", 500, 12, "1.1.1.1")),
    ("send_anomaly_alert", ("c", "/e", 0.9, None, None, None, None, None, None)),
    ("send_api_key_alert", ("created", "id", "n", "me")),
    ("send_api_key_alert", ("revoked", "id", None, None)),
    ("send_error_rate_alert", (0.5, 10, 5, "5m")),
    ("send_traffic_spike_alert", (5.0, 1.0, 5.0, "5m")),
    ("send_request_error_alert", ("E", "/p", "GET", 500, "bad", "rid", "1.1.1.1")),
    ("send_request_error_alert", ("E", "/p", "GET", None, "bad", None, None)),
]


@pytest.mark.parametrize("method,args", SLACK_CALLS)
async def test_slack_alert_builders(method, args):
    s = _slack()
    assert await getattr(s, method)(*args) is True
    payload = s._client.post.call_args.kwargs["json"]
    assert payload["text"]


# ---------------- thresholds ----------------
def test_alert_thresholds_logic(monkeypatch):
    t = n.AlertThresholds()
    t.update(anomaly_score_threshold=0.5, error_rate_threshold=0.2, traffic_spike_threshold=3.0,
             rate_limit_enabled=False, circuit_breaker_enabled=False, min_interval_seconds=7)
    d = t.to_dict()
    assert d["min_interval_seconds"] == 7 and d["rate_limit_enabled"] is False
    assert t.should_alert_anomaly(0.5) and not t.should_alert_anomaly(0.49)
    assert t.should_alert_error_rate(0.2) and not t.should_alert_error_rate(0.1)
    assert t.should_alert_traffic_spike(3) and not t.should_alert_traffic_spike(2.9)
    t.update(anomaly_score_threshold=0, error_rate_threshold=0, traffic_spike_threshold=0)
    assert not (t.should_alert_anomaly(9) or t.should_alert_error_rate(9) or t.should_alert_traffic_spike(9))
    t.update()  # all None: unchanged


# ---------------- manager ----------------
@pytest.fixture
def mgr():
    m = n.NotificationManager()
    m.alert_thresholds.update(min_interval_seconds=0, rate_limit_enabled=True, circuit_breaker_enabled=True,
                              anomaly_score_threshold=0.5, error_rate_threshold=0.1, traffic_spike_threshold=2.0)
    m.email_service = MagicMock()
    m.slack_service = MagicMock()
    for name in ("rate_limit", "circuit_breaker", "anomaly", "api_key", "error_rate", "traffic_spike",
                 "request_error"):
        setattr(m.email_service, f"send_{name}_alert", AsyncMock(return_value=True))
        setattr(m.slack_service, f"send_{name}_alert", AsyncMock(return_value=True))
    m.alert_recipients = ["ops@x.com"]
    return m


MGR_CALLS = [
    ("rate_limit", ("k", "1.1.1.1", 5, "1m", "/p"), {}),
    ("circuit_breaker", ("http://b", "OPEN", "CLOSED", 3, "m"), {}),
    ("anomaly", ("c", "/e", 0.9), {}),
    ("api_key", ("created", "id1"), {}),
    ("error_rate", (0.5, 10, 5), {}),
    ("traffic_spike", (10.0, 2.0, 5.0), {}),
    ("request_error", ("E", "/p", "GET", 500, "bad"), {}),
]


@pytest.mark.parametrize("name,args,kw", MGR_CALLS)
async def test_manager_dispatch_to_both_channels(mgr, name, args, kw):
    await getattr(mgr, f"send_{name}_alert")(*args, **kw)
    getattr(mgr.email_service, f"send_{name}_alert").assert_awaited_once()
    getattr(mgr.slack_service, f"send_{name}_alert").assert_awaited_once()
    assert len(mgr.email_sent_times) == 1 and len(mgr.slack_sent_times) == 1


@pytest.mark.parametrize("name,args,kw", MGR_CALLS)
async def test_manager_dedup_within_interval(mgr, name, args, kw):
    mgr.alert_thresholds.update(min_interval_seconds=3600)
    await getattr(mgr, f"send_{name}_alert")(*args, **kw)
    await getattr(mgr, f"send_{name}_alert")(*args, **kw)
    assert getattr(mgr.slack_service, f"send_{name}_alert").await_count == 1


@pytest.mark.parametrize("name,args,kw", MGR_CALLS)
async def test_manager_rate_limits_block_sends(mgr, name, args, kw):
    mgr.email_rate_limit_per_hour = 1
    mgr.slack_rate_limit_per_hour = 1
    mgr.email_sent_times = [time.time()]
    mgr.slack_sent_times = [time.time()]
    await getattr(mgr, f"send_{name}_alert")(*args, **kw)
    getattr(mgr.email_service, f"send_{name}_alert").assert_not_awaited()
    getattr(mgr.slack_service, f"send_{name}_alert").assert_not_awaited()


async def test_manager_disabled_and_below_threshold(mgr):
    mgr.alert_thresholds.update(rate_limit_enabled=False, circuit_breaker_enabled=False)
    await mgr.send_rate_limit_alert("k", "ip", 1, "1m", "/p")
    await mgr.send_circuit_breaker_alert("b", "OPEN", "CLOSED", 1, "m")
    await mgr.send_anomaly_alert("c", "/e", 0.1)
    await mgr.send_error_rate_alert(0.01, 100, 1)
    await mgr.send_traffic_spike_alert(1, 1, 1.0)
    mgr.email_service.send_rate_limit_alert.assert_not_awaited()
    mgr.slack_service.send_anomaly_alert.assert_not_awaited()
    mgr.slack_service.send_error_rate_alert.assert_not_awaited()
    mgr.slack_service.send_traffic_spike_alert.assert_not_awaited()


async def test_manager_no_recipients_skips_email_only(mgr):
    mgr.alert_recipients = []
    await mgr.send_api_key_alert("created", "id")
    mgr.email_service.send_api_key_alert.assert_not_awaited()
    mgr.slack_service.send_api_key_alert.assert_awaited_once()


async def test_dispatch_failure_not_recorded_and_exceptions_swallowed(mgr):
    mgr.email_service.send_api_key_alert = AsyncMock(return_value=False)
    mgr.slack_service.send_api_key_alert = AsyncMock(side_effect=RuntimeError("down"))
    await mgr.send_api_key_alert("created", "id")  # must not raise
    assert mgr.email_sent_times == [] and mgr.slack_sent_times == []
    await mgr._dispatch("other", AsyncMock(return_value=True))


def test_rate_limit_windows_expire(mgr):
    mgr.email_rate_limit_per_hour = 1
    mgr.email_sent_times = [time.time() - 4000]
    assert mgr._check_email_rate_limit() is True and mgr.email_sent_times == []
    mgr._record_email_sent()
    assert mgr._check_email_rate_limit() is False
    mgr.slack_rate_limit_per_hour = 1
    mgr._record_slack_sent()
    assert mgr._check_slack_rate_limit() is False


def test_manager_configure_and_recipients(monkeypatch):
    m = n.NotificationManager()
    monkeypatch.setattr(n, "settings", types.SimpleNamespace(email_rate_limit_per_hour=7,
                                                             slack_rate_limit_per_hour=9))
    m.configure_email("smtp.x", 25, "u", "p", False, "from@x")
    assert m.email_service.smtp_port == 25 and m.email_rate_limit_per_hour == 7
    m.configure_slack("http://hook", "#c", "bot")
    assert m.slack_service.default_channel == "#c" and m.slack_rate_limit_per_hour == 9
    m.set_alert_recipients(["a@b"])
    assert m.alert_recipients == ["a@b"]
    m.set_alert_recipients_from_string(" a@b , ,c@d ")
    assert m.alert_recipients == ["a@b", "c@d"]
    m.set_alert_recipients_from_string("")
    assert m.alert_recipients == ["a@b", "c@d"]
    m.update_alert_thresholds(error_rate_threshold=0.9)
    assert m.get_alert_thresholds()["error_rate_threshold"] == 0.9
