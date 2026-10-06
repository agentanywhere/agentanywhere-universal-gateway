#!/bin/bash
# PostgreSQL setup script for production deployment

set -e

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

# Wait for PostgreSQL to be ready
echo "⏳ Waiting for PostgreSQL to be ready..."
until PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d postgres -c '\q' 2>/dev/null; do
    echo "   PostgreSQL is unavailable - sleeping"
    sleep 1
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
echo "📝 Update your .env file with:"
echo "   DATABASE_URL=postgresql+asyncpg://$DB_USER:$DB_PASSWORD@$DB_HOST:$DB_PORT/$DB_NAME"
echo ""
echo "🔄 Run migrations:"
echo "   alembic upgrade head"


