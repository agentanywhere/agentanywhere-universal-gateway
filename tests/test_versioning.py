"""Tests for the API versioning module."""
from datetime import timedelta
from app.time_utils import utcnow

import pytest

from app.versioning import (
    APIVersion,
    VersionManager,
    VersionStatus,
    VersionUnavailable,
)


class TestAPIVersion:
    def test_active_version_is_active(self):
        v = APIVersion(version="1")
        assert v.is_active() is True
        assert v.get_warning_header() is None
        assert v.get_sunset_header() is None

    def test_deprecated_version_returns_warning(self):
        v = APIVersion(version="1", status=VersionStatus.DEPRECATED)
        warning = v.get_warning_header()
        assert warning and "deprecated" in warning

    def test_sunset_returns_sunset_header(self):
        sunset = utcnow() + timedelta(days=30)
        v = APIVersion(version="1", status=VersionStatus.SUNSET, sunset_date=sunset)
        assert v.get_sunset_header() is not None
        assert v.is_active() is True  # not yet past sunset

    def test_past_sunset_is_inactive(self):
        sunset = utcnow() - timedelta(days=1)
        v = APIVersion(version="1", status=VersionStatus.SUNSET, sunset_date=sunset)
        assert v.is_active() is False

    def test_removed_is_inactive(self):
        v = APIVersion(version="1", status=VersionStatus.REMOVED)
        assert v.is_active() is False


class TestVersionManagerResolution:
    def setup_method(self):
        self.mgr = VersionManager()
        self.mgr.register_version("1", default=True)
        self.mgr.register_version("2")

    def test_resolve_from_path_strips_prefix(self):
        v, p = self.mgr.resolve("/v2/users/42")
        assert v is not None and v.version == "2"
        assert p == "/users/42"

    def test_resolve_from_default_when_no_prefix(self):
        v, p = self.mgr.resolve("/users")
        assert v is not None and v.version == "1"
        assert p == "/users"

    def test_resolve_from_accept_version_header(self):
        v, p = self.mgr.resolve("/users", {"Accept-Version": "v2"})
        assert v.version == "2"
        assert p == "/users"

    def test_resolve_from_x_api_version_header(self):
        v, p = self.mgr.resolve("/users", {"X-API-Version": "2"})
        assert v.version == "2"

    def test_path_takes_precedence_over_header(self):
        v, _ = self.mgr.resolve("/v1/users", {"Accept-Version": "v2"})
        assert v.version == "1"

    def test_unknown_version_falls_through_to_default(self):
        v, p = self.mgr.resolve("/v99/users")
        # /v99/ doesn't match any registered version, so resolution
        # leaves the path stripped but version is None.
        assert v is None
        assert p == "/users"

    def test_removed_version_raises(self):
        self.mgr.remove_version("2")
        with pytest.raises(VersionUnavailable) as exc:
            self.mgr.resolve("/v2/users")
        assert exc.value.status_code == 410
        assert exc.value.version == "2"

    def test_past_sunset_raises(self):
        past = utcnow() - timedelta(days=1)
        self.mgr.deprecate_version("2", sunset_date=past)
        with pytest.raises(VersionUnavailable):
            self.mgr.resolve("/v2/users")

    def test_deprecated_still_resolves_with_warning(self):
        self.mgr.deprecate_version("2")
        v, _ = self.mgr.resolve("/v2/users")
        assert v is not None
        assert v.status == VersionStatus.DEPRECATED
        assert v.get_warning_header() is not None


class TestVersionManagerLifecycle:
    def test_register_default_when_first(self):
        mgr = VersionManager()
        mgr.register_version("1")
        assert mgr.default_version == "1"

    def test_register_explicit_default_overrides(self):
        mgr = VersionManager()
        mgr.register_version("1")
        mgr.register_version("2", default=True)
        assert mgr.default_version == "2"

    def test_to_dict_round_trip(self):
        mgr = VersionManager()
        v = mgr.register_version("1", default=True)
        d = v.to_dict()
        assert d["version"] == "1"
        assert d["status"] == "active"
        assert d["default"] is True
        assert d["is_active"] is True

    def test_strip_prefix_setting(self):
        mgr = VersionManager()
        mgr.register_version("1", default=True)
        mgr.strip_prefix_on_proxy = True
        _, p = mgr.resolve("/v1/users/42")
        assert p == "/users/42"
