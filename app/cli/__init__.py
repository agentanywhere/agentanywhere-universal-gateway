"""Command-line interface for AgentAnywhere Universal Gateway.

Run via the installed entry point::

    agentanywhere-gateway --help

The legacy ``mcp-gateway`` command is also registered as an alias and
continues to work after the PyPI rename.

Or directly::

    python -m app.cli --help
"""
from app.cli.main import main, build_parser

__all__ = ["main", "build_parser"]
