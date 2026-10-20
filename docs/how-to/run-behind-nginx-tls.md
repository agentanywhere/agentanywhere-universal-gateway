# Run behind nginx with TLS

**Goal:** serve the gateway over HTTPS with the nginx configuration in this
repository, and keep the operational endpoints private.

**Before you start:**

- Docker and Docker Compose, and the gateway set up as in
  [Deploy with Docker Compose](deploy-with-docker-compose.md).
- A certificate and key for your host name, or `openssl` to make a
  self-signed pair for testing.
- Ports 80 and 443 free on the host.

The gateway speaks plain HTTP on port 8000 and does not terminate TLS itself.

## Contents

- [What the repository provides](#what-the-repository-provides)
- [Steps](#steps)
- [Check that it worked](#check-that-it-worked)
- [If it did not work](#if-it-did-not-work)

## What the repository provides

- `nginx/nginx.conf`: an HTTP server on port 80 that redirects to HTTPS, and an
  HTTPS server on 443 (TLS 1.2 and 1.3, HSTS and other security headers) that
  proxies everything to `gateway:8000`. It allows request bodies up to 100M
  and 300-second read timeouts, and passes WebSocket upgrade headers.
- `nginx/Dockerfile`: `nginx:alpine` with that configuration.
- The `nginx` service in `docker-compose.yml`, under the `production` profile,
  which mounts `nginx/ssl/` read-only at `/etc/nginx/ssl` and
  `nginx/nginx.conf` over the image's copy.
- `scripts/generate_ssl_cert.sh`: writes a self-signed `nginx/ssl/cert.pem`
  and `nginx/ssl/key.pem` for `CN=localhost`, valid 365 days.

These files were read for this page; the stack was not started.

## Steps

1. Put the certificate in place. For testing:

   ```bash
   bash scripts/generate_ssl_cert.sh
   ```

   For a real host, copy your certificate chain to `nginx/ssl/cert.pem` and
   the private key to `nginx/ssl/key.pem`. The directory is git-ignored.

2. Restrict the operational endpoints. In `nginx/nginx.conf`, the `/metrics`
   block has an allow-list that is commented out, and `/health/detailed`,
   `/openapi.json`, `/docs` and `/redoc` are served by the catch-all
   `location /`. Edit the HTTPS server block so that it reads, for example:

   ```nginx
   location /metrics {
       allow 10.0.0.0/8;
       deny all;
       proxy_pass http://gateway_backend;
       proxy_http_version 1.1;
       proxy_set_header Host $host;
   }

   location ~ ^/(health/detailed|openapi\.json|docs|redoc) {
       allow 10.0.0.0/8;
       deny all;
       proxy_pass http://gateway_backend;
       proxy_http_version 1.1;
       proxy_set_header Host $host;
   }
   ```

   Use your monitoring network instead of `10.0.0.0/8`. Consider the same for
   `/admin` if operators reach the admin API from a known network.

3. Make the client address trustworthy. The gateway takes the client address
   from the first entry of `X-Forwarded-For`, and `$proxy_add_x_forwarded_for`
   keeps whatever the client sent. In `location /`, replace

   ```nginx
   proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
   ```

   with

   ```nginx
   proxy_set_header X-Forwarded-For $remote_addr;
   ```

   if nginx is the first proxy that clients reach. If there is another proxy in
   front of nginx, set the header to the address that proxy reports, and use
   nginx's `real_ip` module to trust only that proxy.

4. Set `CORS_ORIGINS` in `.env` to the HTTPS origins of your front ends, and
   `GATEWAY_URL_BASE=https://gateway.example.com` so generated SDKs and the
   A2A Agent Card use the public URL.

5. Start the stack with the profile:

   ```bash
   docker compose --profile production up -d --build
   ```

   The `production` profile also starts PostgreSQL. Set `POSTGRES_PASSWORD`
   in `.env` first; see [Deploy with Docker Compose](deploy-with-docker-compose.md#use-postgresql-instead-of-sqlite).

6. Close port 8000 to the outside. The gateway's own port is published on all
   interfaces by `docker-compose.yml` (`"${PORT:-8000}:8000"`); restrict it with
   your firewall, or change the mapping to `"127.0.0.1:8000:8000"`.

## Check that it worked

```bash
curl -sI http://your-host/health        # 301 to https://
curl -s https://your-host/health        # {"status":"healthy",...}
curl -s -o /dev/null -w "%{http_code}\n" https://your-host/metrics   # 403 from outside the allow-list
```

With a self-signed certificate, add `-k` to the `curl` commands.

## If it did not work

| Symptom | Cause | Fix |
|---|---|---|
| nginx exits at start-up with `cannot load certificate` | `nginx/ssl/cert.pem` or `key.pem` is missing | Run `scripts/generate_ssl_cert.sh` or copy your files. |
| 502 from nginx | The gateway container is not healthy | `docker compose ps`, then `docker compose logs gateway`. |
| 413 Request Entity Too Large | A body over 100M, or a smaller limit in another proxy | Raise `client_max_body_size` on every hop. |
| Streams cut off after 300 seconds | `proxy_read_timeout 300s` | Raise it for the streaming paths. |
| IP rules match the wrong address | `X-Forwarded-For` taken as sent | Step 3. |
| Browser shows a certificate warning | Self-signed certificate | Use a certificate from your CA. |
