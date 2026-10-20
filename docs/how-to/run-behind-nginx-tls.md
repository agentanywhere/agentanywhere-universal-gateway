# Run behind nginx with TLS

**Goal:** serve the gateway over HTTPS with the nginx configuration in this
repository, keep the operational endpoints private, and make the gateway see
real client addresses.

**Before you start:**

- Docker and Docker Compose, and the gateway set up as in
  [Deploy with Docker Compose](deploy-with-docker-compose.md).
- A certificate and key for your host name, or `openssl` to make a
  self-signed pair for testing.
- Ports 80 and 443 free on the host.

The gateway speaks plain HTTP on port 8000 and does not terminate TLS itself.
The files below were read for this page; the stack was not started.

## Contents

- [What the repository provides](#what-the-repository-provides)
- [Steps](#steps)
- [Check that it worked](#check-that-it-worked)
- [If it did not work](#if-it-did-not-work)

## What the repository provides

- `nginx/nginx.conf`: a server-level file (`upstream` and `server` blocks).
  `nginx/Dockerfile` and the `nginx` service in `docker-compose.yml` install it
  as `/etc/nginx/conf.d/default.conf`, inside the stock `http {}` block. It
  has:
  - an HTTP server on port 80 that redirects everything to HTTPS;
  - an HTTPS server on 443 (TLS 1.2 and 1.3, HSTS and other security headers)
    that proxies to `gateway:8000`, allows bodies up to 100M and reads of 300
    seconds, and passes WebSocket upgrade headers;
  - `X-Forwarded-For` **overwritten** with `$remote_addr`, so a client cannot
    choose it;
  - `/metrics` and `/health/detailed` answered only for `127.0.0.1`, `::1`,
    `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16` and `fc00::/7`; everyone
    else gets 403.
- The `nginx` service, under the `production` profile, which mounts
  `nginx/ssl/` read-only at `/etc/nginx/ssl`.
- `scripts/generate_ssl_cert.sh`: writes a self-signed `nginx/ssl/cert.pem`
  and `nginx/ssl/key.pem` for `CN=localhost`, valid 365 days.

## Steps

1. Put the certificate in place. For testing:

   ```bash
   bash scripts/generate_ssl_cert.sh
   ```

   For a real host, copy your certificate chain to `nginx/ssl/cert.pem` and
   the private key to `nginx/ssl/key.pem`. The directory is git-ignored.

2. Adjust who may read `/metrics` and `/health/detailed`. Edit the `allow`
   lines in **both** locations of `nginx/nginx.conf` (one address or CIDR per
   line) and keep `deny all;` last. Two things to know:
   - the check uses the address nginx sees; if another proxy or load balancer
     is in front, configure `set_real_ip_from` and `real_ip_header` first;
   - connections relayed by Docker's userland proxy appear to come from the
     bridge gateway, which is inside `172.16.0.0/12` and therefore allowed.
     Remove that range, or rely on step 5.

3. Tell the gateway to trust nginx for `X-Forwarded-For`. In `.env`:

   ```bash
   TRUSTED_PROXIES=172.18.0.0/16
   ```

   Use the subnet of the Compose network
   (`docker network inspect <project>_gateway-network`), or nginx's address.
   Without this, IP rules, logs and metrics see every request as coming from
   nginx. A whole subnet also trusts anything else on it, including
   connections to port 8000 relayed from the bridge gateway, so close port
   8000 (step 5) when you list a subnet.

4. Set `CORS_ORIGINS` to the HTTPS origins of your front ends, and
   `GATEWAY_URL_BASE=https://gateway.example.com` so generated SDKs and the
   A2A Agent Card use the public URL.

5. Close the gateway's own port. `docker-compose.yml` publishes port 8000 on
   all interfaces, and clients there bypass nginx and its allow-list.
   Firewall it, or change the mapping to `"127.0.0.1:8000:8000"`. For a check
   that does not depend on the network path, also set
   `OPS_ENDPOINTS_REQUIRE_ADMIN=true` and give Prometheus the admin token (the
   commented `authorization:` block in `prometheus.yml`).

6. Set `POSTGRES_PASSWORD` in `.env` (the `production` profile also starts
   PostgreSQL, which refuses to start without it), then start:

   ```bash
   docker compose --profile production up -d --build
   ```

## Check that it worked

```bash
curl -sI http://your-host/health        # 301 to https://
curl -s https://your-host/health        # {"status":"healthy",...}
curl -s -o /dev/null -w "%{http_code}\n" https://your-host/metrics   # 403 from outside the allow-list
```

With a self-signed certificate, add `-k`. `docker compose ps` may show the
`nginx` service as unhealthy even when it works: its healthcheck requests
`http://localhost/health`, which the port-80 server redirects
([known limitations](../known-limitations.md#the-nginx-healthcheck-probably-reports-unhealthy)).

## If it did not work

| Symptom | Cause | Fix |
|---|---|---|
| nginx exits at start-up with `cannot load certificate` | `nginx/ssl/cert.pem` or `key.pem` is missing | Run `scripts/generate_ssl_cert.sh` or copy your files. |
| 502 from nginx | The gateway container is not healthy | `docker compose ps`, then `docker compose logs gateway`. |
| 413 Request Entity Too Large | A body over 100M, or a smaller limit in another proxy | Raise `client_max_body_size` on every hop. |
| Streams cut off after 300 seconds | `proxy_read_timeout 300s` | Raise it for the streaming paths. |
| Logs and IP rules show nginx's address for every client | `TRUSTED_PROXIES` is empty | Step 3. |
| `/metrics` answers 403 to Prometheus | Prometheus is outside the allow-list | Add its address to both `allow` lists, or scrape `gateway:8000` on the Compose network. |
| Browser shows a certificate warning | Self-signed certificate | Use a certificate from your CA. |
