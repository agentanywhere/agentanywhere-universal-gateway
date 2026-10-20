# Send events to a webhook

**Goal:** have the gateway POST a JSON message to your URL when something
happens, such as a key hitting its rate limit, a backend's circuit breaker
opening, or an API key being created or revoked.

**Before you start:** an HTTPS endpoint that accepts POST requests, the admin
credential in `ADMIN` and the gateway address in `URL`. Webhooks are kept **in
memory**: they disappear when the gateway restarts, and with several worker
processes each one has its own list. Register them from your deployment
tooling after every start.

This page is written from `app/webhooks.py`, the admin endpoints in
`admin/api.py` and `tests/test_webhooks.py`. Registration and the
`rate_limit_exceeded` event were run in process for this page; delivery to a
live endpoint was not.

## Events that are actually sent

| Event type | Sent when | `data` includes |
|---|---|---|
| `rate_limit_exceeded` | A key exceeds its own per-minute or per-hour limit | `api_key_id`, `api_key_name`, `client_ip`, `limit`, `window`, `path` |
| `circuit_breaker_open` | A backend's breaker opens | `backend_url`, `failure_count`, `failure_threshold`, `opened_at`, `timeout_seconds`, `message` |
| `circuit_breaker_closed` | A half-open breaker closes after a success | `backend_url`, `previous_state`, `message` |
| `api_key_created` | `POST /admin/api-keys` | `api_key_id`, `name`, limits |
| `api_key_revoked` | `DELETE /admin/api-keys/{id}` | `api_key_id` |
| `anomaly_detected` | The in-process anomaly detector flags a request | Anomaly details |

`request_error`, `high_error_rate` and `traffic_spike` are listed by
`GET /admin/webhooks/events` but nothing sends them in this release. The
endpoint also accepts names it does not know (a plugin may send its own
events) and lists them in the response's `unknown_events`; the built-in code
never sends those.

## Steps

1. Register the webhook, as a JSON body (query parameters `url`,
   `event_type` and `secret` work too):

   ```bash
   curl -s -X POST "$URL/admin/webhooks" -H "Authorization: Bearer $ADMIN" \
     -H "Content-Type: application/json" \
     -d "{\"url\": \"https://hooks.example.com/gateway\", \"events\": [\"rate_limit_exceeded\", \"circuit_breaker_open\"], \"secret\": \"$WEBHOOK_SECRET\"}"
   ```

   One webhook is registered per event; the answer lists their ids in `ids`
   (and the first in `id`). Optional `headers` and `filters` go in the same
   body. Check that the answer has no `unknown_events`.

   With the CLI:
   `agentanywhere-gateway webhooks create --target-url https://hooks.example.com/gateway --events rate_limit_exceeded circuit_breaker_open --secret "$WEBHOOK_SECRET"`.

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

```bash
curl -s -X DELETE "$URL/admin/webhooks/$WEBHOOK_ID" -H "Authorization: Bearer $ADMIN"
```

or `agentanywhere-gateway webhooks delete <id>`.

## If it did not work

| Symptom | Cause | Fix |
|---|---|---|
| 422 on register | `url` or an event is missing | Send `url` and `event_type` or `events`. |
| `unknown_events` in the answer | A misspelt event name | Use a name from `GET /admin/webhooks/events`. |
| The webhook is gone | The gateway restarted, or another worker answered | Re-register after each start; run one process. |
| No deliveries for 429s | The 429 came from a tenant quota, or no webhook is registered for `rate_limit_exceeded` | Check the 429's `details` and `GET /admin/webhooks`. |
| Signature does not match | Different serialisation | Remove `signature`, serialise with sorted keys and Python's default separators (`", "` and `": "`), then compute HMAC-SHA256. |
