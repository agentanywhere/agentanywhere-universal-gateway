#!/bin/bash
# PostgreSQL setup script for production deployment

set -euo pipefail

DB_NAME="${POSTGRES_DB:-gateway}"
DB_USER="${POSTGRES_USER:-gateway_user}"
DB_PASSWORD="${POSTGRES_PASSWORD:-}"
DB_HOST="${POSTGRES_HOST:-localhost}"
DB_PORT="${POSTGRES_PORT:-5432}"

echo "🐘 Setting up PostgreSQL database..."

if [ -z "$DB_PASSWORD" ]; then
    echo "❌ Error: POSTGRES_PASSWORD environment variable is required"
    exit 1
fi

if ! command -v psql >/dev/null 2>&1; then
    echo "❌ Error: psql (the PostgreSQL client) is not installed"
    exit 1
fi

# Wait for PostgreSQL to be ready, but not forever.
WAIT_SECONDS="${POSTGRES_WAIT_SECONDS:-60}"
echo "⏳ Waiting up to ${WAIT_SECONDS}s for PostgreSQL to be ready..."
waited=0
until PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d postgres -c '\q' 2>/dev/null; do
    if [ "$waited" -ge "$WAIT_SECONDS" ]; then
        echo "❌ Error: PostgreSQL at $DB_HOST:$DB_PORT is not reachable as $DB_USER after ${WAIT_SECONDS}s"
        exit 1
    fi
    echo "   PostgreSQL is unavailable - sleeping"
    sleep 1
    waited=$((waited + 1))
done

echo "✅ PostgreSQL is ready"

# Create database if it doesn't exist
echo "📦 Creating database '$DB_NAME' if it doesn't exist..."
PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d postgres <<EOF
SELECT 'CREATE DATABASE $DB_NAME'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '$DB_NAME')\gexec
EOF

echo "✅ Database setup complete!"
echo ""
echo "📝 Next steps:"
echo "   1. Set DATABASE_URL in your .env file (the password is not printed here):"
echo "      DATABASE_URL=postgresql+asyncpg://$DB_USER:<POSTGRES_PASSWORD>@$DB_HOST:$DB_PORT/$DB_NAME"
echo "   2. Create the tables, from the repository root, with DATABASE_URL set:"
echo "      alembic upgrade head"
echo "      (Or just start the gateway: it creates missing tables itself. Run"
echo "      'alembic upgrade head' before every upgrade either way; see docs/UPGRADING.md.)"


