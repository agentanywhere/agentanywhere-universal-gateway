#!/bin/bash
# Production setup script
# This script sets up all production hardening features

set -euo pipefail

USE_POSTGRES="${USE_POSTGRES:-false}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-}"

echo "🚀 Setting up production environment..."
echo ""

# Check if running as root (for some operations)
if [ "${EUID:-$(id -u)}" -eq 0 ]; then 
    echo "⚠️  Running as root. Some operations may require elevated privileges."
fi

# 1. Generate secret key
echo "1️⃣  Generating production secret key..."
if [ -f ".env" ] && grep -q "SECRET_KEY=change-me-in-production" .env; then
    python3 scripts/generate_secret_key.py --update-env
    echo "   ✅ Secret key generated and updated in .env"
elif [ -f ".env" ] && grep -qE '^[[:space:]]*SECRET_KEY=.+' .env; then
    echo "   ℹ️  SECRET_KEY is already set in .env; left unchanged"
else
    SECRET_KEY=$(python3 scripts/generate_secret_key.py)
    echo "   ✅ Secret key generated: ${SECRET_KEY:0:20}..."
    echo "   ⚠️  Add this to your .env file: SECRET_KEY=$SECRET_KEY"
fi
echo ""

# 2. Generate SSL certificates (for development/testing)
echo "2️⃣  Setting up SSL certificates..."
if [ ! -f "nginx/ssl/cert.pem" ] || [ ! -f "nginx/ssl/key.pem" ]; then
    bash scripts/generate_ssl_cert.sh
    echo "   ✅ SSL certificates generated"
else
    echo "   ℹ️  SSL certificates already exist"
fi
echo ""

# 3. Setup PostgreSQL (if enabled)
if [ "$USE_POSTGRES" = "true" ]; then
    echo "3️⃣  Setting up PostgreSQL..."
    if [ -z "$POSTGRES_PASSWORD" ]; then
        echo "   ❌ USE_POSTGRES=true but POSTGRES_PASSWORD is not set."
        exit 1
    fi
    if ! bash scripts/setup_postgres.sh; then
        echo "   ❌ PostgreSQL setup failed; see the error above."
        exit 1
    fi
    echo "   ✅ PostgreSQL setup complete"
else
    echo "3️⃣  PostgreSQL setup skipped (set USE_POSTGRES=true to enable)"
fi
echo ""

# 4. Setup log rotation
echo "4️⃣  Setting up log rotation..."
mkdir -p logs
if command -v logrotate &> /dev/null; then
    echo "   ✅ logrotate is available"
    echo "   📝 Add to crontab: 0 0 * * * $(pwd)/scripts/rotate_logs.sh"
else
    echo "   ⚠️  logrotate not found. Manual rotation script available."
    echo "   Run: bash scripts/rotate_logs.sh"
fi
echo ""

# 5. Check Docker resource limits
echo "5️⃣  Checking Docker configuration..."
if grep -q "deploy:" docker-compose.yml; then
    echo "   ✅ Resource limits configured in docker-compose.yml"
else
    echo "   ⚠️  Resource limits not found in docker-compose.yml"
fi
echo ""

# 6. Run database migrations (if PostgreSQL is enabled)
if [ "$USE_POSTGRES" = "true" ]; then
    echo "6️⃣  Running database migrations..."
    # Uses DATABASE_URL (environment or .env), like the gateway. Safe on an
    # empty database and on one the gateway already created; see docs/UPGRADING.md.
    # It must point at PostgreSQL, or the migrations would go to the default
    # SQLite file instead.
    db_url="${DATABASE_URL:-}"
    if [ -z "$db_url" ] && [ -f .env ]; then
        db_url="$(grep -E '^[[:space:]]*DATABASE_URL=' .env | tail -n 1 | cut -d= -f2- || true)"
    fi
    case "$db_url" in
        postgresql*) ;;
        *)
            echo "   ❌ DATABASE_URL is not a PostgreSQL URL (environment or .env)."
            echo "      Set it as shown by scripts/setup_postgres.sh, then run this script again."
            exit 1
            ;;
    esac
    if ! command -v alembic &> /dev/null; then
        echo "   ❌ Alembic not found. Install the requirements: pip install -r requirements.txt"
        exit 1
    fi
    if alembic upgrade head; then
        echo "   ✅ Database migrations complete"
    else
        echo "   ❌ alembic upgrade head failed; see the error above and docs/UPGRADING.md"
        exit 1
    fi
else
    echo "6️⃣  Database migrations skipped (SQLite will be used)"
fi
echo ""

echo "✅ Production setup complete!"
echo ""
echo "📝 Next steps:"
echo "   1. Review and update .env file with production values"
echo "   2. For production HTTPS, replace self-signed certs with Let's Encrypt:"
echo "      certbot --nginx -d yourdomain.com"
echo "   3. Start services with production profile:"
echo "      docker compose --profile production up -d"
echo "   4. Monitor logs: docker compose logs -f gateway"
echo ""


