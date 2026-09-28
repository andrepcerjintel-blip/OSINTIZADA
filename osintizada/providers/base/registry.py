"""Registro de providers (arquitetura plugin-like).

Adicionar uma fonte nova exige apenas:
  1. criar a classe do provider;
  2. decorá-la com ``@register_provider``;
  3. declarar ``supported_identifiers``.
"""

from __future__ import annotations

import importlib
import pkgutil

from osintizada.config import Settings
from osintizada.core.enums import IdentifierType
from osintizada.providers.base.provider import BaseProvider
from osintizada.resilience import ProviderRuntime


class ProviderRegistry:
    def __init__(self) -> None:
        self._classes: dict[str, type[BaseProvider]] = {}

    def register(self, cls: type[BaseProvider]) -> type[BaseProvider]:
        name = getattr(cls, "name", None)
        if not name:
            raise ValueError(f"{cls.__name__} precisa declarar 'name'")
        existing = self._classes.get(name)
        if existing is not None and existing is not cls:
            raise ValueError(f"Provider duplicado: {name}")
        self._classes[name] = cls
        return cls

    def names(self) -> list[str]:
        return sorted(self._classes)

    def get_class(self, name: str) -> type[BaseProvider]:
        return self._classes[name]

    def create_all(self, settings: Settings | None = None, runtime: ProviderRuntime | None = None) -> list[BaseProvider]:
        runtime = runtime or ProviderRuntime()
        return [cls(settings, runtime) for _, cls in sorted(self._classes.items())]

    def classes_for(self, identifier_type: IdentifierType) -> list[type[BaseProvider]]:
        return [c for c in self._classes.values() if identifier_type in c.supported_identifiers]

    def __contains__(self, name: str) -> bool:
        return name in self._classes

    def __len__(self) -> int:
        return len(self._classes)


default_registry = ProviderRegistry()


def register_provider(cls: type[BaseProvider]) -> type[BaseProvider]:
    return default_registry.register(cls)


_BUILTIN_PACKAGES = (
    "osintizada.providers.local",
    "osintizada.providers.search",
    "osintizada.providers.infrastructure",
    "osintizada.providers.archive",
    "osintizada.providers.telegram",
)


def load_builtin_providers() -> ProviderRegistry:
    """Importa os pacotes de providers embutidos para acionar o registro."""
    for package_name in _BUILTIN_PACKAGES:
        package = importlib.import_module(package_name)
        for module in pkgutil.iter_modules(package.__path__):
            importlib.import_module(f"{package_name}.{module.name}")
    return default_registry
