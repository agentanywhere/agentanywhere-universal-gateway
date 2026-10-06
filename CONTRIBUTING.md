# Contributing to AgentAnywhere Universal Gateway

Thank you for your interest in contributing to AgentAnywhere Universal Gateway! This document provides guidelines and instructions for contributing.

## Code of Conduct

By participating in this project, you agree to abide by our
[Code of Conduct](CODE_OF_CONDUCT.md), which helps keep the community respectful
and inclusive for everyone.

## Security

Please **do not** file public issues for security vulnerabilities. Follow the
private reporting process in [SECURITY.md](SECURITY.md) instead.

## How to Contribute

### Reporting Issues

- Use the GitHub issue tracker to report bugs or request features (there are
  [issue templates](.github/ISSUE_TEMPLATE/) for bugs and feature requests)
- Include:
  - Clear description of the issue
  - Steps to reproduce (for bugs)
  - Expected vs actual behavior
  - Environment details (OS, Python version, etc.)
  - Relevant logs or error messages

### Submitting Pull Requests

1. **Fork the repository** and create a feature branch
   ```bash
   git checkout -b feature/your-feature-name
   ```

2. **Make your changes**
   - Follow the existing code style
   - Add tests for new features
   - Update documentation as needed
   - Ensure all tests pass

3. **Commit your changes**
   ```bash
   git commit -m "feat: add your feature description"
   ```
   Use conventional commit messages:
   - `feat:` for new features
   - `fix:` for bug fixes
   - `docs:` for documentation
   - `test:` for tests
   - `refactor:` for code refactoring
   - `chore:` for maintenance tasks

4. **Push and create a Pull Request**
   ```bash
   git push origin feature/your-feature-name
   ```

5. **Wait for review** - Maintainers will review your PR and provide feedback

## Development Setup

### Prerequisites

- Python 3.11+
- Docker and Docker Compose
- Redis (for rate limiting)
- PostgreSQL (optional, SQLite works for development)

### Local Development

1. **Clone the repository**
   ```bash
   git clone https://github.com/agentanywhere/universal-gateway.git
   cd universal-gateway
   ```

2. **Set up virtual environment**
   ```bash
   python3 -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   pip install -r requirements.txt
   ```

3. **Set up environment variables**
   ```bash
   cp env.example .env
   # Edit .env with your configuration
   ```

4. **Run tests**
   ```bash
   pytest tests/ -v
   pytest tests/ --cov=app --cov=admin
   ```

5. **Run locally**
   ```bash
   uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
   ```

## Code Style

- Follow PEP 8 for Python code
- Use type hints where possible
- Write docstrings for all functions and classes
- Keep functions focused and small
- Use meaningful variable names

## Testing

- Write tests for all new features
- Aim for high test coverage
- Include both unit and integration tests
- Test edge cases and error conditions
- See [docs/TESTING.md](docs/TESTING.md) for what CI runs.

Run tests:
```bash
# All tests
pytest tests/ -v

# With coverage
pytest tests/ --cov=app --cov=admin --cov-report=html

# Specific test file
pytest tests/test_gateway.py -v
```

## Documentation

- Update README.md for user-facing changes
- Add docstrings to new functions/classes
- Update API documentation if endpoints change
- Add examples for new features

## Project Structure

```
mcp_gateway/
├── app/              # Main application code
├── admin/            # Admin API endpoints
├── admin-ui/         # React admin console
├── tests/            # Test suite
├── docs/             # Documentation
├── examples/         # Example code and use cases
├── cookbooks/        # Integration recipes
└── scripts/          # Utility scripts
```

## Protocol Support

When adding support for new protocols:

1. Create handler in `app/protocols/`
2. Add protocol type to `app/models.py`
3. Update routing logic in `app/routing.py`
4. Add tests in `tests/test_protocols.py`
5. Update documentation

## AI Features

When adding AI-powered features:

1. Implement in `app/ai/`
2. Integrate with `app/ai_service.py`
3. Add admin endpoints in `admin/api.py`
4. Update admin UI if needed
5. Add tests and benchmarks

## Questions?

- Open a GitHub Discussion for questions
- Check existing issues and PRs

Thank you for contributing! 🚀


