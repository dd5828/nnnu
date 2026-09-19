"""能力注册表（§6.11）：manifest + 工厂，进程全局单例，注册幂等。"""

import logging
from typing import Callable

from nnnu.core.capability_protocol import BaseCapability, CapabilityManifest

logger = logging.getLogger(__name__)


class CapabilityRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, Callable[[], BaseCapability]] = {}

    def register(
        self,
        manifest: CapabilityManifest,
        factory: Callable[[], BaseCapability],
        *,
        replace: bool = True,
    ) -> None:
        if manifest.name in self._factories:
            if not replace:
                raise ValueError(f"能力 {manifest.name} 已注册")
            logger.debug("能力 %s 重复注册，已替换", manifest.name)
        self._factories[manifest.name] = factory

    def get(self, name: str) -> BaseCapability | None:
        factory = self._factories.get(name)
        return factory() if factory else None

    def manifests(self) -> list[CapabilityManifest]:
        return [factory().manifest for factory in self._factories.values()]


_capability_registry: CapabilityRegistry | None = None


def get_capability_registry() -> CapabilityRegistry:
    global _capability_registry
    if _capability_registry is None:
        _capability_registry = CapabilityRegistry()
    return _capability_registry
