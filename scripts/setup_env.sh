#!/bin/bash
# Setup .env file from env.example

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

ENV_EXAMPLE="$PROJECT_ROOT/env.example"
ENV_FILE="$PROJECT_ROOT/.env"

echo "🔧 Setting up .env file..."
echo ""

if [ -f "$ENV_FILE" ]; then
    echo "⚠️  .env file already exists!"
    read -p "Do you want to overwrite it? (y/N): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Aborted. Keeping existing .env file."
        exit 0
    fi
    echo ""
fi

# Copy env.example to .env
cp "$ENV_EXAMPLE" "$ENV_FILE"

echo "✅ Created .env file from env.example"
echo ""
echo "📝 Next steps:"
echo "   1. Edit .env file with your configuration:"
echo "      $EDITOR $ENV_FILE"
echo ""
echo "   2. Generate a secure secret key:"
echo "      python3 scripts/generate_secret_key.py --update-env"
echo ""
echo "   3. Update backend URLs and other settings as needed"
echo ""
echo "⚠️  Important: Never commit .env file to version control!"


