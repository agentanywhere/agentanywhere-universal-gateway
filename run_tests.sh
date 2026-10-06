#!/bin/bash
# Comprehensive test runner for AgentAnywhere Universal Gateway

set -e

echo "🧪 AgentAnywhere Universal Gateway Test Suite"
echo "=============================="
echo ""

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Check if pytest is installed
if ! python3 -m pytest --version > /dev/null 2>&1; then
    echo "Installing test dependencies..."
    python3 -m pip install --quiet pytest pytest-asyncio httpx structlog pydantic
fi

echo "Running test suite..."
echo ""

# Run tests with detailed output
python3 -m pytest tests/ -v --tb=short --maxfail=10

# Get summary
echo ""
echo "📊 Test Summary"
echo "=============="
python3 -m pytest tests/ --tb=no -q

echo ""
echo "✅ Test run complete!"


