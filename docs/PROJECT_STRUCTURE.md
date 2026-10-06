# Project Structure

> Reference material. It is kept in step with the code by the automated tests and by
> review, not by a line-by-line manual check. If this page and the gateway disagree,
> the gateway is right; please open an issue.

This document describes the organization and structure of the AgentAnywhere Universal Gateway project.

## Directory Structure

```
mcp_gateway/
├── admin/                    # Admin API endpoints
│   ├── __init__.py
│   └── api.py               # Admin API router
│
├── admin-ui/                 # Admin Console Frontend (React/TypeScript)
│   ├── src/
│   │   ├── api/             # API client
│   │   ├── components/      # React components
│   │   ├── contexts/        # React contexts (Auth, Theme)
│   │   └── pages/           # Page components
│   ├── Dockerfile
│   └── package.json
│
├── alembic/                  # Database migrations
│   ├── versions/
│   └── env.py
│
├── app/                      # Main application code
│   ├── ai/                   # AI/ML features
│   │   ├── anomaly_detector.py
│   │   ├── cost_optimizer.py
│   │   ├── intelligent_rate_limiter.py
│   │   ├── predictive_analytics.py
│   │   └── scaling_recommender.py
│   │
│   ├── middleware/           # HTTP middleware
│   │   ├── auth.py          # Authentication middleware
│   │   └── rate_limit.py    # Rate limiting middleware
│   │
│   ├── protocols/           # Protocol handlers
│   │   ├── graphql_handler.py
│   │   ├── grpc_handler.py
│   │   ├── mqtt_handler.py
│   │   └── websocket_handler.py
│   │
│   ├── repositories/        # Data access layer
│   │   ├── api_key_repo.py
│   │   ├── log_repo.py
│   │   ├── route_repo.py
│   │   └── settings_profile_repo.py
│   │
│   ├── sdk/                 # SDK generators
│   │   ├── generator.py
│   │   ├── go_generator.py
│   │   ├── javascript_generator.py
│   │   └── python_generator.py
│   │
│   ├── config.py           # Configuration management
│   ├── database.py         # Database setup
│   ├── db_models.py        # SQLAlchemy models
│   ├── log_exporters.py    # Log export to external systems
│   ├── logging_config.py   # Logging configuration
│   ├── main.py             # FastAPI application entry point
│   ├── metrics.py          # Prometheus metrics
│   ├── models.py           # Pydantic models
│   ├── notifications.py    # Email notifications
│   ├── oauth2.py           # OAuth2 integration
│   ├── routing.py          # Request routing logic
│   ├── tenant.py            # Multi-tenant support
│   ├── transformers.py     # Request/response transformation
│   ├── versioning.py       # API versioning
│   └── webhooks.py         # Webhook management
│
├── docs/                    # Documentation
│   ├── README.md            # Index of all guides
│   ├── TESTING.md           # Pytest and CI
│   ├── PROJECT_STRUCTURE.md
│   ├── PRODUCTION_SETUP.md
│   └── ...
├── .github/
│   └── workflows/           # ci.yml, docker-publish.yml (GitHub Actions)
│
├── nginx/                   # Nginx reverse proxy configuration
│   ├── Dockerfile
│   ├── nginx.conf
│   └── ssl/                 # SSL certificates
│
├── scripts/                 # Utility scripts
│   ├── generate_secret_key.py
│   ├── generate_ssl_cert.sh
│   ├── rotate_logs.sh
│   ├── setup_postgres.sh
│   └── setup_production.sh
│
├── tests/                   # Test suite
│   ├── results/             # Test result logs
│   ├── conftest.py         # Pytest configuration
│   ├── test_admin_api.py
│   ├── test_auth.py
│   ├── test_gateway.py
│   ├── test_protocols.py
│   └── ...
│
├── logs/                    # Application logs (gitignored)
│
├── .gitignore
├── alembic.ini
├── docker-compose.yml
├── Dockerfile
├── env.example
├── Makefile
├── prometheus.yml
├── pytest.ini
├── README.md
└── requirements.txt
```

## Key Principles

### 1. Separation of Concerns
- **app/**: Core application logic
- **admin/**: Admin API endpoints (separate from main app)
- **tests/**: All test code
- **docs/**: All documentation

### 2. No Test Code in Production Routes
- Test endpoints should NOT be in `app/main.py`
- All testing should be done via the `tests/` directory
- Use proper test fixtures and mocks

### 3. Protocol Handlers
- Each protocol has its own handler in `app/protocols/`
- Handlers are modular and can be enabled/disabled via configuration

### 4. Data Access Layer
- All database operations go through repositories in `app/repositories/`
- Models are separated: `app/models.py` (Pydantic) and `app/db_models.py` (SQLAlchemy)

### 5. Configuration
- Environment-based configuration via `app/config.py`
- Example environment file: `env.example`
- Never commit `.env` files

### 6. Documentation
- All documentation in `docs/` folder
- README.md in root for quick start
- Detailed docs in `docs/` subdirectory

## File Naming Conventions

- **Python files**: `snake_case.py`
- **Test files**: `test_*.py`
- **Config files**: `*.ini`, `*.yml`, `*.yaml`
- **Scripts**: `*.sh` (bash) or `*.py` (Python)

## Module Organization

### Core Modules
- `app/main.py`: FastAPI application, middleware, route handlers
- `app/routing.py`: Request routing logic
- `app/config.py`: Configuration management
- `app/database.py`: Database connection and setup

### Feature Modules
- `app/webhooks.py`: Webhook management
- `app/oauth2.py`: OAuth2 integration
- `app/tenant.py`: Multi-tenant support
- `app/notifications.py`: Email notifications
- `app/log_exporters.py`: Log export to external systems
- `app/transformers.py`: Request/response transformation
- `app/versioning.py`: API versioning

### AI/ML Modules
- `app/ai/`: All AI-powered features
  - Anomaly detection
  - Intelligent rate limiting
  - Cost optimization
  - Predictive analytics
  - Auto-scaling recommendations

## Testing Structure

```
tests/
├── conftest.py              # Pytest fixtures and configuration
├── test_admin_api.py        # Admin API tests
├── test_auth.py             # Authentication tests
├── test_gateway.py          # Gateway functionality tests
├── test_protocols.py        # Protocol handler tests
├── test_routing.py          # Routing logic tests
└── results/                 # Test output logs (gitignored)
```

## Best Practices

1. **Never put test endpoints in production code**
2. **Keep routes clean and focused**
3. **Use dependency injection for testability**
4. **Separate concerns: routing, business logic, data access**
5. **Document public APIs**
6. **Follow PEP 8 for Python code**
7. **Use type hints for better IDE support**


