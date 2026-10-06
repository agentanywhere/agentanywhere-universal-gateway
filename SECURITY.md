# Security Policy

## Supported versions

| Version | Supported |
|---------|-----------|
| 1.0.x   | ✅        |
| < 1.0   | ❌        |

## Reporting a vulnerability

**Please do not open public GitHub issues for security vulnerabilities.**

Report suspected vulnerabilities privately to **security@agentanywhere.ai**
(or use GitHub's [private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
on this repository). Please include:

- A description of the issue and its impact.
- Steps to reproduce (proof-of-concept if possible).
- Affected version(s) and configuration.

### What to expect

- **Acknowledgement** within 3 business days.
- An initial assessment and severity rating within 10 business days.
- Coordinated disclosure: we will work with you on a fix and a disclosure
  timeline, and credit you (if you wish) in the release notes.

## Hardening checklist for operators

The gateway is security-sensitive infrastructure. When deploying:

- Set a strong, unique `SECRET_KEY` (see `scripts/generate_secret_key.py`).
  The app refuses to treat the default value as production-safe.
- Terminate TLS in front of the gateway (reverse proxy) — see
  [docs/PRODUCTION_SETUP.md](docs/PRODUCTION_SETUP.md).
- Scope API keys with `allowed_paths` and set expiries (`expires_at` /
  `expires_in_days`).
- Use PostgreSQL and a dedicated Redis in production.
- Keep dependencies patched; the CI pipeline builds and tests on every push.
- Never commit secrets. `.env`, credentials, and infra files are git-ignored.
