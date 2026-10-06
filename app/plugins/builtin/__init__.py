"""Built-in example plugins shipped with the gateway.

These plugins are intentionally simple. They serve as reference
implementations for third parties and provide a useful baseline for
operators (request ID stamping, header injection, basic logging hook).

All plugins ship disabled by default would they be intrusive; opt-in
ones default to enabled and are documented in ``docs/PLUGINS.md``.
"""

from app.plugins.builtin.request_id import RequestIDPlugin
from app.plugins.builtin.header_injector import HeaderInjectorPlugin
from app.plugins.builtin.audit_logger import AuditLoggerPlugin
from app.plugins.builtin.static_token_auth import StaticTokenAuthPlugin

__all__ = [
    "RequestIDPlugin",
    "HeaderInjectorPlugin",
    "AuditLoggerPlugin",
    "StaticTokenAuthPlugin",
]
