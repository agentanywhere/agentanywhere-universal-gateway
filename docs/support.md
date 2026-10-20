# Support and community

## Where to ask

| You want to | Go to |
|---|---|
| Report a bug | A GitHub issue on this repository, using the bug report template in `.github/ISSUE_TEMPLATE/bug_report.md`. |
| Ask for a feature | A GitHub issue, using the feature request template. |
| Ask a question about using the gateway | A GitHub issue. Check the [FAQ](faq.md), [troubleshooting](troubleshooting.md) and [known limitations](known-limitations.md) first. |
| Report a security vulnerability | **Not** a public issue. Follow [SECURITY.md](../SECURITY.md): email `security@agentanywhere.ai`, or use GitHub's private vulnerability reporting. |
| Report a code of conduct concern | `conduct@agentanywhere.ai`, as described in the [code of conduct](../CODE_OF_CONDUCT.md). |
| Contribute code or documentation | Read [CONTRIBUTING.md](../CONTRIBUTING.md), then open a pull request. |

The repository does not promise response times for issues. SECURITY.md states
the response targets for vulnerability reports.

## What to include in a bug report

- What you ran (commands, request, configuration with secrets removed).
- What you expected and what happened, including the full error body.
- The `request_id` from the error body or the `X-Request-ID` header, and the
  matching gateway log lines (with keys and tokens removed).
- Versions: the git commit or tag, `python3 --version`, and whether you run
  from source, the Docker image or Docker Compose.
- Whether Redis and PostgreSQL are in use.

Do not paste API keys, `SECRET_KEY`, `ADMIN_TOKEN`, database URLs with
passwords, or `GET /admin/config` output (it contains `ADMIN_TOKEN`).

## Documentation problems

If a page and the gateway disagree, the gateway is right and the page has a
bug. Open an issue naming the page and what you saw, or a pull request that
fixes it ([CONTRIBUTING.md](../CONTRIBUTING.md#documentation)).
