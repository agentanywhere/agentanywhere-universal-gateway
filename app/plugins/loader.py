"""Discover and load plugins.

Plugins can come from three sources, all of which are tried at startup:

1. The built-in :mod:`app.plugins.builtin` package — example plugins
   shipped with the gateway.
2. Installed packages exposing the ``mcp_gateway.plugins`` entry point
   (Python's standard mechanism for third-party extensions).
3. Filesystem directories listed in ``MCP_GATEWAY_PLUGIN_PATH`` or in
   ``settings.plugin_paths`` — useful for local development and Docker
   bind mounts.
"""
from __future__ import annotations

import importlib
import importlib.util
import inspect
import os
import pkgutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Type

import structlog

from app.plugins.base import Plugin, PluginType
from app.plugins.registry import PluginRegistry, plugin_registry

logger = structlog.get_logger()

ENV_PLUGIN_PATH = "MCP_GATEWAY_PLUGIN_PATH"
ENV_PLUGIN_DISABLED = "MCP_GATEWAY_PLUGIN_DISABLED"
ENTRY_POINT_GROUP = "mcp_gateway.plugins"
BUILTIN_PACKAGE = "app.plugins.builtin"


@dataclass
class LoadResult:
    """Summary of a load operation, surfaced by the admin API / CLI."""

    loaded: List[str]
    skipped: List[str]
    errors: List[Dict[str, str]]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "loaded": list(self.loaded),
            "skipped": list(self.skipped),
            "errors": list(self.errors),
        }


class PluginLoader:
    """Discovers Plugin subclasses from packages, entry points, and dirs."""

    def __init__(
        self,
        registry: Optional[PluginRegistry] = None,
        *,
        plugin_paths: Optional[Sequence[str]] = None,
        disabled: Optional[Iterable[str]] = None,
        plugin_configs: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> None:
        self.registry = registry or plugin_registry
        env_paths = os.environ.get(ENV_PLUGIN_PATH, "")
        env_disabled = os.environ.get(ENV_PLUGIN_DISABLED, "")
        self.plugin_paths: List[str] = [
            p.strip() for p in (list(plugin_paths or []) + env_paths.split(os.pathsep)) if p.strip()
        ]
        self.disabled = {d.strip() for d in (list(disabled or []) + env_disabled.split(",")) if d.strip()}
        self.plugin_configs: Dict[str, Dict[str, Any]] = dict(plugin_configs or {})

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover(self) -> List[Type[Plugin]]:
        classes: List[Type[Plugin]] = []
        classes.extend(self._discover_builtin())
        classes.extend(self._discover_entry_points())
        for path in self.plugin_paths:
            classes.extend(self._discover_directory(path))
        return _dedupe_classes(classes)

    def _discover_builtin(self) -> List[Type[Plugin]]:
        try:
            pkg = importlib.import_module(BUILTIN_PACKAGE)
        except Exception as exc:
            logger.warning("plugin_builtin_import_failed", error=str(exc))
            return []
        return list(_iter_plugin_classes_from_package(pkg))

    def _discover_entry_points(self) -> List[Type[Plugin]]:
        classes: List[Type[Plugin]] = []
        try:
            from importlib.metadata import entry_points
        except ImportError:  # pragma: no cover - py<3.8
            return classes
        try:
            eps = entry_points()
            if hasattr(eps, "select"):
                selected = eps.select(group=ENTRY_POINT_GROUP)
            else:  # pragma: no cover - older API
                selected = eps.get(ENTRY_POINT_GROUP, [])
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("plugin_entry_points_failed", error=str(exc))
            return classes
        for ep in selected:
            try:
                obj = ep.load()
                if inspect.isclass(obj) and issubclass(obj, Plugin):
                    classes.append(obj)
                elif inspect.ismodule(obj):
                    classes.extend(_iter_plugin_classes(obj))
                else:
                    logger.warning("plugin_entry_point_unsupported", name=ep.name)
            except Exception as exc:
                logger.error("plugin_entry_point_load_failed", name=ep.name, error=str(exc))
        return classes

    def _discover_directory(self, path: str) -> List[Type[Plugin]]:
        directory = Path(path).expanduser().resolve()
        if not directory.is_dir():
            logger.warning("plugin_dir_missing", path=str(directory))
            return []

        classes: List[Type[Plugin]] = []
        if str(directory) not in sys.path:
            sys.path.insert(0, str(directory))
        for file in directory.glob("*.py"):
            if file.name.startswith("_"):
                continue
            module_name = f"_mcp_plugin_{file.stem}"
            try:
                spec = importlib.util.spec_from_file_location(module_name, file)
                if spec is None or spec.loader is None:
                    continue
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
                classes.extend(_iter_plugin_classes(module))
            except Exception as exc:
                logger.error("plugin_file_load_failed", path=str(file), error=str(exc))
        return classes

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def load(self) -> LoadResult:
        loaded: List[str] = []
        skipped: List[str] = []
        errors: List[Dict[str, str]] = []
        for cls in self.discover():
            name = getattr(cls.metadata, "name", cls.__name__)
            if not name or name == "unnamed":
                name = cls.__name__
            if name in self.disabled:
                skipped.append(name)
                continue
            try:
                config = self.plugin_configs.get(name, {})
                instance = cls(config=config)
                instance.metadata.name = name
                self.registry.register(instance, replace=True)
                loaded.append(name)
            except Exception as exc:
                errors.append({"plugin": name, "error": str(exc)})
                logger.error("plugin_load_failed", plugin=name, error=str(exc))
        logger.info(
            "plugin_loader_complete",
            loaded=len(loaded),
            skipped=len(skipped),
            errors=len(errors),
        )
        return LoadResult(loaded=loaded, skipped=skipped, errors=errors)


def load_plugins(
    *,
    plugin_paths: Optional[Sequence[str]] = None,
    disabled: Optional[Iterable[str]] = None,
    plugin_configs: Optional[Dict[str, Dict[str, Any]]] = None,
    registry: Optional[PluginRegistry] = None,
) -> LoadResult:
    """Convenience helper to load plugins with the default registry."""
    loader = PluginLoader(
        registry=registry,
        plugin_paths=plugin_paths,
        disabled=disabled,
        plugin_configs=plugin_configs,
    )
    return loader.load()


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _iter_plugin_classes(module) -> List[Type[Plugin]]:
    out: List[Type[Plugin]] = []
    for _, obj in inspect.getmembers(module, inspect.isclass):
        if obj is Plugin:
            continue
        if not issubclass(obj, Plugin):
            continue
        if obj.__module__ != module.__name__:
            continue
        out.append(obj)
    return out


def _iter_plugin_classes_from_package(package) -> List[Type[Plugin]]:
    out: List[Type[Plugin]] = []
    if not hasattr(package, "__path__"):
        return _iter_plugin_classes(package)
    for _, modname, _ in pkgutil.walk_packages(package.__path__, prefix=package.__name__ + "."):
        try:
            module = importlib.import_module(modname)
        except Exception as exc:
            logger.warning("plugin_module_import_failed", module=modname, error=str(exc))
            continue
        out.extend(_iter_plugin_classes(module))
    return out


def _dedupe_classes(classes: Sequence[Type[Plugin]]) -> List[Type[Plugin]]:
    seen: Dict[str, Type[Plugin]] = {}
    for cls in classes:
        key = f"{cls.__module__}.{cls.__name__}"
        if key not in seen:
            seen[key] = cls
    return list(seen.values())
