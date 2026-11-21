"""API versioning support.

Supports URL-based versioning (``/v1/...``) plus header-based version
hints via ``Accept-Version`` or ``X-API-Version``. Versions can be
deprecated (with a ``Warning`` header), scheduled for sunset (with an
``Sunset`` header per RFC 8594) or removed (rejected with HTTP 410).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from app.time_utils import utcnow
from email.utils import format_datetime
from enum import Enum
from typing import Dict, List, Optional, Tuple

import structlog

logger = structlog.get_logger()


VERSION_PATH_RE = re.compile(r"^/v(?P<version>\d+(?:\.\d+)?)(?P<rest>/.*|$)")


class VersionStatus(str, Enum):
    """API version status."""

    ACTIVE = "active"
    DEPRECATED = "deprecated"
    SUNSET = "sunset"  # Scheduled for removal
    REMOVED = "removed"


class VersionUnavailable(Exception):
    """Raised when a request targets a version that is no longer available."""

    def __init__(self, version: str, message: str, status_code: int = 410):
        super().__init__(message)
        self.version = version
        self.message = message
        self.status_code = status_code


class APIVersion:
    """Represents an API version."""

    def __init__(
        self,
        version: str,
        status: VersionStatus = VersionStatus.ACTIVE,
        deprecated_date: Optional[datetime] = None,
        sunset_date: Optional[datetime] = None,
        base_path: Optional[str] = None,
        default: bool = False,
        backend_url: Optional[str] = None,
    ):
        self.version = version
        self.status = status
        self.deprecated_date = deprecated_date
        self.sunset_date = sunset_date
        self.base_path = base_path or f"/v{version}"
        self.default = default
        self.backend_url = backend_url
        self.created_at = utcnow()

    def is_active(self) -> bool:
        """True if the version still accepts traffic."""
        if self.status == VersionStatus.REMOVED:
            return False
        if self.status == VersionStatus.SUNSET and self.sunset_date:
            return not self.sunset_passed()
        return True

    def sunset_passed(self) -> bool:
        """True once the sunset date is reached.

        A date given with a UTC offset (``2027-01-01T00:00:00+05:30``, or one
        ending in ``Z``) is compared as the moment it names. One given without
        is taken as UTC, as before.
        """
        when = self.sunset_date
        if when is None:
            return False
        if when.tzinfo is not None:
            when = when.astimezone(timezone.utc).replace(tzinfo=None)
        return utcnow() >= when

    def get_warning_header(self) -> Optional[str]:
        """RFC 7234 Warning header for deprecated/sunset versions."""
        if self.status in (VersionStatus.DEPRECATED, VersionStatus.SUNSET):
            warning = f'299 - "API version {self.version} is deprecated"'
            if self.sunset_date:
                warning += f', date="{self.sunset_date.isoformat()}"'
            return warning
        return None

    def get_sunset_header(self) -> Optional[str]:
        """RFC 8594 Sunset header value (HTTP date format)."""
        if self.sunset_date:
            try:
                return format_datetime(self.sunset_date)
            except Exception:
                return self.sunset_date.isoformat()
        return None

    def to_dict(self) -> Dict[str, object]:
        return {
            "version": self.version,
            "status": self.status.value,
            "base_path": self.base_path,
            "default": self.default,
            "backend_url": self.backend_url,
            "deprecated_date": self.deprecated_date.isoformat() if self.deprecated_date else None,
            "sunset_date": self.sunset_date.isoformat() if self.sunset_date else None,
            "is_active": self.is_active(),
        }


class VersionManager:
    """Manages API versions."""

    def __init__(self):
        self.versions: Dict[str, APIVersion] = {}
        self.default_version: Optional[str] = None
        # When True, the version prefix is stripped from the path before
        # proxying to the backend (e.g. ``/v1/users`` -> ``/users``).
        self.strip_prefix_on_proxy: bool = False

    # ------------------------------------------------------------------
    # Registration / lifecycle
    # ------------------------------------------------------------------

    def register_version(
        self,
        version: str,
        status: VersionStatus = VersionStatus.ACTIVE,
        deprecated_date: Optional[datetime] = None,
        sunset_date: Optional[datetime] = None,
        base_path: Optional[str] = None,
        default: bool = False,
        backend_url: Optional[str] = None,
    ) -> APIVersion:
        api_version = APIVersion(
            version=version,
            status=status,
            deprecated_date=deprecated_date,
            sunset_date=sunset_date,
            base_path=base_path,
            default=default,
            backend_url=backend_url,
        )

        self.versions[version] = api_version

        if default or not self.default_version:
            self.default_version = version

        logger.info(
            "api_version_registered",
            version=version,
            status=status.value,
            sunset=sunset_date.isoformat() if sunset_date else None,
        )
        return api_version

    def get_version(self, version: str) -> Optional[APIVersion]:
        return self.versions.get(version)

    def get_default_version(self) -> Optional[APIVersion]:
        if self.default_version:
            return self.versions.get(self.default_version)
        return None

    def list_versions(self) -> List[APIVersion]:
        return list(self.versions.values())

    def deprecate_version(self, version: str, sunset_date: Optional[datetime] = None):
        if version not in self.versions:
            return
        v = self.versions[version]
        v.status = VersionStatus.DEPRECATED
        v.deprecated_date = utcnow()
        if sunset_date:
            v.sunset_date = sunset_date
            v.status = VersionStatus.SUNSET
        logger.info("api_version_deprecated", version=version, sunset_date=sunset_date)

    def remove_version(self, version: str):
        if version in self.versions:
            self.versions[version].status = VersionStatus.REMOVED
            logger.info("api_version_removed", version=version)

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def get_version_from_path(self, path: str) -> Optional[APIVersion]:
        """Backwards-compatible: returns the version matched by path or default."""
        m = VERSION_PATH_RE.match(path)
        if m:
            return self.versions.get(m.group("version"))
        if self.default_version:
            return self.versions.get(self.default_version)
        return None

    def get_version_from_headers(self, headers: Dict[str, str]) -> Optional[APIVersion]:
        """Look up version from Accept-Version / X-API-Version headers."""
        if not headers:
            return None
        # Case-insensitive lookup.
        lower = {k.lower(): v for k, v in headers.items()}
        candidate = lower.get("accept-version") or lower.get("x-api-version")
        if not candidate:
            return None
        # Allow values like "v2", "v2.1", "2", "2.1".
        candidate = candidate.strip().lstrip("vV")
        return self.versions.get(candidate)

    def resolve(
        self,
        path: str,
        headers: Optional[Dict[str, str]] = None,
    ) -> Tuple[Optional[APIVersion], str]:
        """Resolve the API version for a request.

        Returns a tuple of ``(version, path_without_prefix)``. If the
        version is removed or its sunset date has passed, raises
        ``VersionUnavailable``.
        """
        api_version: Optional[APIVersion] = None
        stripped = path

        m = VERSION_PATH_RE.match(path)
        if m:
            ver = m.group("version")
            api_version = self.versions.get(ver)
            rest = m.group("rest") or "/"
            stripped = rest if rest.startswith("/") else "/" + rest
        else:
            if headers:
                api_version = self.get_version_from_headers(headers)
            if not api_version and self.default_version:
                api_version = self.versions.get(self.default_version)

        if api_version is not None:
            if api_version.status == VersionStatus.REMOVED:
                raise VersionUnavailable(
                    api_version.version,
                    f"API version {api_version.version} has been removed",
                    status_code=410,
                )
            if (
                api_version.status == VersionStatus.SUNSET
                and api_version.sunset_passed()
            ):
                raise VersionUnavailable(
                    api_version.version,
                    f"API version {api_version.version} is no longer available",
                    status_code=410,
                )

        return api_version, stripped


version_manager = VersionManager()

# Seed sensible defaults; can be overridden / extended at runtime.
version_manager.register_version("1", default=True)
version_manager.register_version("2")
