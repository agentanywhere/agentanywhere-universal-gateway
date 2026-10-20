# Documentation

Start with the [quick start](QUICKSTART.md). Its commands are run by hand
before each release, as are the request examples in [PROTOCOLS.md](PROTOCOLS.md).

The other pages are reference material. They are kept in step with the code by
the automated tests and by review, not by a line-by-line manual check, so if a
page and the gateway disagree, the gateway is right and the page has a bug.
Please open an issue.

- [QUICKSTART.md](QUICKSTART.md): install, run, first request
- [PROTOCOLS.md](PROTOCOLS.md): each protocol's route, backend setting and sample backend, and which ones work today
- [CONFIGURATION.md](CONFIGURATION.md): settings
- [API_REFERENCE.md](API_REFERENCE.md): gateway and admin endpoints
- [CLI.md](CLI.md): the `agentanywhere-gateway` command
- [PLUGINS.md](PLUGINS.md): writing a plugin
- [TESTING.md](TESTING.md): running the tests, and what CI runs
- [DEPLOYMENT.md](DEPLOYMENT.md): container, Compose, reverse proxy, more than one process
- [PRODUCTION_SETUP.md](PRODUCTION_SETUP.md): production checklist, defaults to change, known limitations
- [UPGRADING.md](UPGRADING.md): how the database schema is created, and how to run the Alembic migrations
- [WEBHOOK_TESTING.md](WEBHOOK_TESTING.md): trying out webhooks
- [PROJECT_STRUCTURE.md](PROJECT_STRUCTURE.md): where things live in the repository
