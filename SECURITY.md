# Security Policy

## Supported versions

| Version | Supported |
|---------|-----------|
| 1.0.x   | Yes       |
| < 1.0   | No        |

Version 1.0.0 is not yet released; see the [changelog](CHANGELOG.md).

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

## Known security limitations

Some weaknesses are known and documented, with workarounds, in
[docs/known-limitations.md](docs/known-limitations.md). Among them: API keys
are stored in plaintext, `SECRET_KEY` is accepted on both the admin API and the
data plane, CORS allows every origin by default, `/metrics` and
`/health/detailed` need no credential, key path scoping (`allowed_paths`) and
admin roles are not enforced, and a revoked key can keep working for up to five
minutes while it is cached in Redis. You do not need to report these again,
but reports of new consequences are welcome.

## Hardening checklist for operators

The gateway is security-sensitive infrastructure. The full checklist is in
[Secure the gateway for production](docs/how-to/secure-for-production.md). In
short:

- Set `SECRET_KEY` and `ADMIN_TOKEN` to two different random values
  (`python3 scripts/generate_secret_key.py`). The placeholder `SECRET_KEY` from
  `env.example` is refused as a credential; with `ENVIRONMENT=production` the
  gateway prints a warning but still starts.
- Give operators `ADMIN_TOKEN` only, and keep `SECRET_KEY` in your secret store.
- Terminate TLS in front of the gateway, and block `/metrics`,
  `/health/detailed`, `/openapi.json`, `/docs` and `/redoc` from the internet
  ([Run behind nginx with TLS](docs/how-to/run-behind-nginx-tls.md)).
- Set `CORS_ORIGINS` to your real origins.
- Give API keys an expiry (`expires_at` or `expires_in_days`) and rotate them.
- Use PostgreSQL and a dedicated, private Redis, and treat the database, its
  backups and Redis as secrets.
- Keep dependencies patched; CI builds and tests on every push.
- Never commit secrets. `.env`, certificates and keys are git-ignored.

How credentials work is explained in the
[security model](docs/explanation/security-model.md).
