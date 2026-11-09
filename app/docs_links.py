"""Where error responses point callers for help.

The interactive API docs at ``/docs`` only exist when ``DEBUG=true``, so
otherwise the hint links to the same topic in the project documentation.
"""
from app.config import get_settings

_REPO_DOCS = "https://github.com/agentanywhere/agentanywhere-universal-gateway/blob/main/docs"

_TOPICS = {
    "authentication": "how-to/manage-api-keys.md",
    "configuration": "reference/configuration.md",
    "troubleshooting": "troubleshooting.md",
}


def docs_link(topic: str) -> str:
    if get_settings().debug:
        return f"/docs#{topic}"
    return f"{_REPO_DOCS}/{_TOPICS[topic]}"
