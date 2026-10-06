# Production Hardening Guide

> Reference material. It is kept in step with the code by the automated tests and by
> review, not by a line-by-line manual check. If this page and the gateway disagree,
> the gateway is right; please open an issue.

This guide covers all production hardening features implemented for the AgentAnywhere Universal Gateway.

## ✅ Implemented Features

### 1. HTTPS/TLS Configuration

**Nginx Reverse Proxy** with SSL/TLS termination:

- **Location**: `nginx/nginx.conf`
- **Features**:
  - HTTP to HTTPS redirect
  - Modern TLS 1.2/1.3 configuration
  - Security headers (HSTS, X-Frame-Options, etc.)
  - WebSocket support
  - Load balancing ready
  - Health check endpoint

**Setup**:

```bash
# Generate self-signed certs for development/testing
bash scripts/generate_ssl_cert.sh

# For production, use Let's Encrypt:
certbot --nginx -d yourdomain.com
```

**Start with HTTPS**:

```bash
docker-compose --profile production up -d
```

Access via:
- HTTP: http://localhost (redirects to HTTPS)
- HTTPS: https://localhost

### 2. PostgreSQL Migration

**Alembic Migration System**:

- **Location**: `alembic/`
- **Initial Migration**: `alembic/versions/001_initial_schema.py`
- **Setup Script**: `scripts/setup_postgres.sh`

**Setup PostgreSQL**:

```bash
# Set environment variables
export POSTGRES_DB=gateway
export POSTGRES_USER=gateway_user
export POSTGRES_PASSWORD=your-secure-password
export USE_POSTGRES=true

# Run setup script
bash scripts/setup_postgres.sh

# Update .env
echo "DATABASE_URL=postgresql+asyncpg://gateway_user:your-secure-password@localhost:5432/gateway" >> .env

# Run migrations
alembic upgrade head
```

**Start with PostgreSQL**:

```bash
docker-compose --profile production up -d
```

### 3. Log Rotation

**Automatic Log Rotation**:

- **Script**: `scripts/rotate_logs.sh`
- **Configuration**: 
  - Retention: 30 days (configurable via `RETENTION_DAYS`)
  - Max size: 100M (configurable via `MAX_SIZE`)

**Setup**:

```bash
# Manual rotation
bash scripts/rotate_logs.sh

# Automatic rotation (add to crontab)
0 0 * * * /path/to/mcp_gateway/scripts/rotate_logs.sh
```

**Using logrotate** (if available):

The script automatically uses `logrotate` if installed, otherwise falls back to manual rotation.

### 4. Resource Limits in Docker

**Configured in `docker-compose.yml`**:

All services have resource limits:

- **Gateway**:
  - Limits: 2 CPU, 2GB RAM
  - Reservations: 0.5 CPU, 512MB RAM

- **Redis**:
  - Limits: 1 CPU, 512MB RAM
  - Reservations: 0.25 CPU, 128MB RAM

- **PostgreSQL**:
  - Limits: 1 CPU, 1GB RAM
  - Reservations: 0.25 CPU, 256MB RAM

- **Nginx**:
  - Limits: 0.5 CPU, 256MB RAM
  - Reservations: 0.1 CPU, 64MB RAM

- **Admin UI**:
  - Limits: 1 CPU, 512MB RAM
  - Reservations: 0.25 CPU, 128MB RAM

### 5. Production Secret Key Generation

**Enhanced Script**: `scripts/generate_secret_key.py`

**Usage**:

```bash
# Generate and display key
python3 scripts/generate_secret_key.py

# Generate and automatically update .env file
python3 scripts/generate_secret_key.py --update-env
```

**Or use the production setup script**:

```bash
bash scripts/setup_production.sh
```

## 🚀 Quick Production Setup

Run the all-in-one setup script:

```bash
# Set production environment variables
export POSTGRES_PASSWORD=your-secure-password
export USE_POSTGRES=true  # Optional, for PostgreSQL

# Run setup
bash scripts/setup_production.sh
```

This will:
1. ✅ Generate and update secret key
2. ✅ Generate SSL certificates
3. ✅ Setup PostgreSQL (if enabled)
4. ✅ Configure log rotation
5. ✅ Run database migrations

## 📋 Production Checklist

Before deploying to production:

- [ ] **Secret Key**: Generated and set in `.env`
- [ ] **HTTPS**: SSL certificates configured (Let's Encrypt recommended)
- [ ] **Database**: PostgreSQL configured and migrations run
- [ ] **Log Rotation**: Cron job configured
- [ ] **Resource Limits**: Verified in `docker-compose.yml`
- [ ] **Environment Variables**: All production values set
- [ ] **Monitoring**: Prometheus/Grafana configured
- [ ] **Backups**: Database backup strategy in place
- [ ] **Security**: Firewall rules configured
- [ ] **Health Checks**: Verified endpoints responding

## 🔒 Security Best Practices

1. **Never commit** `.env` file or SSL private keys
2. **Use strong passwords** for PostgreSQL and Redis
3. **Enable firewall** and restrict access to necessary ports only
4. **Regular updates** of Docker images and dependencies
5. **Monitor logs** for suspicious activity
6. **Use Let's Encrypt** for production SSL certificates
7. **Enable rate limiting** (already configured)
8. **Use API keys** for authentication (already implemented)

## 📊 Monitoring

### Health Checks

- Gateway: `http://localhost:8000/health`
- Nginx: `http://localhost/health`
- PostgreSQL: Automatic via Docker healthcheck
- Redis: Automatic via Docker healthcheck

### Metrics

- Prometheus: `http://localhost:9090` (with monitoring profile)
- Gateway Metrics: `http://localhost:8000/metrics`

## 🐛 Troubleshooting

### SSL Certificate Issues

```bash
# Regenerate certificates
rm -rf nginx/ssl/*
bash scripts/generate_ssl_cert.sh
docker-compose restart nginx
```

### Database Connection Issues

```bash
# Check PostgreSQL status
docker-compose ps postgres

# Check logs
docker-compose logs postgres

# Test connection
docker-compose exec postgres psql -U gateway_user -d gateway
```

### Log Rotation Issues

```bash
# Check log directory permissions
ls -la logs/

# Manual rotation
bash scripts/rotate_logs.sh

# Check disk space
df -h
```

## 📚 Additional Resources

- [Nginx SSL Configuration](https://nginx.org/en/docs/http/configuring_https_servers.html)
- [Alembic Documentation](https://alembic.sqlalchemy.org/)
- [Docker Resource Limits](https://docs.docker.com/config/containers/resource_constraints/)
- [Let's Encrypt](https://letsencrypt.org/)

