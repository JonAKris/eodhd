"""
core.registry
=============
The agent's roster. Strategies register by name; modes iterate the registry
rather than hard-coding which strategies exist. Adding a strategy to the agent
is one line in `default_registry()`, and every mode picks it up.
"""
from __future__ import annotations

from .contract import Strategy


class Registry:
    """Name -> Strategy. Names must be unique (they're how the CLI selects one)."""

    def __init__(self) -> None:
        self._by_name: dict[str, Strategy] = {}

    def register(self, strategy: Strategy) -> "Registry":
        if strategy.name in self._by_name:
            raise ValueError(f"duplicate strategy name: {strategy.name!r}")
        self._by_name[strategy.name] = strategy
        return self

    def get(self, name: str) -> Strategy:
        try:
            return self._by_name[name]
        except KeyError:
            raise KeyError(
                f"no strategy {name!r}. Registered: {', '.join(self.names()) or '(none)'}"
            )

    def names(self) -> list[str]:
        return list(self._by_name)

    def all(self) -> list[Strategy]:
        return list(self._by_name.values())


def default_registry() -> Registry:
    """The five conformers. Imported lazily so registering doesn't drag in
    ssg_screener (the SSG wrap imports it only when actually evaluated)."""
    from ..strategies.momentum import MomentumStrategy
    from ..strategies.value import ValueStrategy
    from ..strategies.insider import InsiderStrategy
    from ..strategies.institutional_flow import InstitutionalFlowStrategy
    from ..strategies.ssg import SSGStrategy

    reg = Registry()
    for s in (MomentumStrategy(), ValueStrategy(), InsiderStrategy(),
              InstitutionalFlowStrategy(), SSGStrategy()):
        reg.register(s)
    return reg
