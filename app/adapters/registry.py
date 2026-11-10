"""A named registry of adapter factories for one port.

A factory is any callable that takes the settings and returns an adapter. The
setting that picks an adapter is either a registered name (``orjson``) or a
``module:attribute`` path (``my_pkg.fast:make_client``), so a replacement that
lives outside this repository needs no change here.
"""
from __future__ import annotations

import importlib
from typing import Any, Callable, Dict, Generic, List, Type, TypeVar

T = TypeVar("T")


class AdapterError(RuntimeError):
    """The configured adapter does not exist, cannot be loaded, or does not fit its port."""


class AdapterRegistry(Generic[T]):
    def __init__(self, kind: str, port: Type[T]):
        self.kind = kind
        self.port = port
        self._factories: Dict[str, Callable[[Any], T]] = {}

    def register(self, name: str) -> Callable[[Callable[[Any], T]], Callable[[Any], T]]:
        def decorator(factory: Callable[[Any], T]) -> Callable[[Any], T]:
            self._factories[name.lower()] = factory
            return factory

        return decorator

    def names(self) -> List[str]:
        return sorted(self._factories)

    def create(self, name: str, settings: Any) -> T:
        key = (name or "").strip()
        factory = self._factories.get(key.lower())
        if factory is None and ":" in key:
            factory = self._load(key)
        if factory is None:
            raise AdapterError(
                f"Unknown {self.kind} adapter '{name}'. Choose one of: {', '.join(self.names())}, "
                f"or give a 'module:attribute' path to your own factory."
            )
        try:
            adapter = factory(settings)
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(f"The {self.kind} adapter '{name}' failed to start: {exc}") from exc
        if not isinstance(adapter, self.port):
            raise AdapterError(
                f"The {self.kind} adapter '{name}' does not fit the {self.port.__name__} port "
                f"(see app/adapters/ports.py for the methods it needs)."
            )
        return adapter

    def _load(self, path: str) -> Callable[[Any], T]:
        module_name, _, attr = path.partition(":")
        try:
            return getattr(importlib.import_module(module_name), attr)
        except (ImportError, AttributeError) as exc:
            raise AdapterError(f"Cannot load the {self.kind} adapter '{path}': {exc}") from exc
