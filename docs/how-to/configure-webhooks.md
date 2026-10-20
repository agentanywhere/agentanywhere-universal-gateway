# Send events to a webhook

**Goal:** have the gateway POST a JSON message to your URL when something
happens, such as a backend's circuit breaker opening or an API key being
created or revoked.

**Before you start:** an HTTPS endpoint that accepts POST requests, the admin
credential in `ADMIN` and the gateway address in `URL`. Webhooks are kept **in
memory**: they disappear when the gateway restarts, and with several worker
processes each one has its own list. Register them from your deployment
tooling after every start.

This page is written from `app/webhooks.py`, the admin endpoints in
`admin/api.py` and `tests/test_webhooks.py`; it was not run against a live
endpoint.

## Events that are actually sent

| Event type | Sent when | `data` includes |
|---|---|---|
| `circuit_breaker_open` | A backend's breaker opens | `backend_url`, `failure_count`, `failure_threshold`, `opened_at`, `timeout_seconds`, `message` |
| `circuit_breaker_closed` | A half-open breaker closes after a success | `backend_url`, `previous_state`, `message` |
| `api_key_created` | `POST /admin/api-keys` | `api_key_id`, `name`, limits |
| `api_key_revoked` | `DELETE /admin/api-keys/{id}` | `api_key_id` |
| `anomaly_detected` | The in-process anomaly detector flags a request | Anomaly details |

`rate_limit_exceeded`, `request_error`, `high_error_rate` and
`traffic_spike` are listed by `GET /admin/webhooks/events` but are not sent in
this release. For `rate_limit_exceeded` this was reproduced: the rate limiter
builds the event from the key's `id`, which the key object it holds does not
have, and the resulting error is swallowed. The rate-limit email and Slack
alerts are skipped for the same reason.

## Steps

1. Register one webhook per event type. `event_type`, `url` and `secret` are
   **query parameters**:

   ```bash
   curl -s -X POST "$URL/admin/webhooks?event_type=circuit_breaker_open&url=https://hooks.example.com/gateway&secret=$WEBHOOK_SECRET" \
     -H "Authorization: Bearer $ADMIN"
   ```

   The answer contains the webhook's `id`. Optional extra request headers and
   filters go in a JSON body: `{"headers": {"X-Team": "platform"}}`.

2. Verify the signature in your receiver. The gateway sends:

   ```json
   {
     "event_type": "circuit_breaker_open",
     "timestamp": "2026-10-20T17:30:00.000000",
     "data": {"backend_url": "http://http-backend:8003", "...": "..."},
     "signature": "<hex>"
   }
   ```

   (Illustrative.) `signature` is HMAC-SHA256, keyed with your secret, over the
   JSON of the other three fields serialised with sorted keys
   (`json.dumps(payload, sort_keys=True)` in Python). It is a field in the body,
   not a header. Recompute it the same way and compare in constant time.

**Check:**

```bash
curl -s "$URL/admin/webhooks" -H "Authorization: Bearer $ADMIN"
```

lists the webhook with `failure_count` and `last_success`. To see a delivery,
create and revoke a test key, which sends `api_key_created` and
`api_key_revoked` if you registered those.

## Delivery behaviour

- Each delivery is tried up to three times, waiting one and then two seconds
  between attempts, with a ten-second timeout per attempt.
- After ten consecutive failures the webhook is disabled.
- Deliveries are made from the gateway process; a slow receiver does not delay
  the request that caused the event, except for `api_key_created` and
  `api_key_revoked`, which the admin call waits for.

## Remove a webhook

`DELETE /admin/webhooks/{id}` needs the event type as a query parameter:

```bash
curl -s -X DELETE "$URL/admin/webhooks/$WEBHOOK_ID?event_type=circuit_breaker_open" \
  -H "Authorization: Bearer $ADMIN"
```

The CLI's `webhooks create` and `webhooks delete` do not send the parameters
the endpoints need; use `curl`.

## If it did not work

| Symptom | Cause | Fix |
|---|---|---|
| 422 on register | Parameters sent as JSON | Put `event_type`, `url` and `secret` in the query string. |
| The webhook is gone | The gateway restarted, or another worker answered | Re-register after each start; run one process. |
| No deliveries for 429s | `rate_limit_exceeded` is not sent in this release | Count 429 responses at your reverse proxy. |
| Signature does not match | Different serialisation | Remove `signature`, serialise with sorted keys and Python's default separators (`", "` and `": "`), then compute HMAC-SHA256. |
