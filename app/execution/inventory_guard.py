"""Inventory constitution guard (user directive, 2026-08-24).

INVENTORY_CONSTITUTION_ENABLED = false is the hard default
(app.config.settings) and this module is the ONLY place allowed to
decide "yes, a real inventory-constitution BUY may proceed" — every
inventory-purchase path must route through
assert_inventory_constitution_allowed() first and treat
InventoryExecutionRefused as final.

Deliberately SEPARATE from app.execution.live_guard.live_guard: buying
inventory and executing the arbitrage that inventory unblocks are two
independently-authorized actions — "pour le premier test uniquement, ne
lance pas encore automatiquement l'arbitrage" (user directive,
2026-08-24). Flipping live_trading_enabled would authorize BOTH actions
at once; this guard authorizes ONLY the inventory purchase, and nothing
in this codebase ever flips either flag itself.
"""

import time


class InventoryExecutionRefused(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class InventoryConstitutionGuard:
    def __init__(
        self,
        inventory_constitution_enabled: bool,
        max_usdt_per_asset: float,
        max_concurrent_operations: int = 1,
    ) -> None:
        self._enabled = inventory_constitution_enabled
        self._max_usdt_per_asset = max_usdt_per_asset
        self._max_concurrent_operations = max_concurrent_operations
        self._in_flight_count = 0
        self._kill_switch_engaged = False
        self._kill_switch_reason: str | None = None
        self._kill_switch_at: float | None = None

    @property
    def inventory_constitution_enabled(self) -> bool:
        return self._enabled

    @property
    def max_usdt_per_asset(self) -> float:
        return self._max_usdt_per_asset

    @property
    def kill_switch_engaged(self) -> bool:
        return self._kill_switch_engaged

    @property
    def kill_switch_reason(self) -> str | None:
        return self._kill_switch_reason

    @property
    def in_flight_count(self) -> int:
        return self._in_flight_count

    def engage_kill_switch(self, reason: str, now: float | None = None) -> None:
        self._kill_switch_engaged = True
        self._kill_switch_reason = reason
        self._kill_switch_at = now if now is not None else time.time()

    def disengage_kill_switch(self) -> None:
        self._kill_switch_engaged = False
        self._kill_switch_reason = None
        self._kill_switch_at = None

    def assert_inventory_constitution_allowed(self, symbol: str, sell_exchange: str, requested_usdt: float) -> None:
        """Raises InventoryExecutionRefused for any reason at all to
        proceed — callers must never catch this and continue, only
        report it and stop. SPOT-only by construction: sell_exchange
        must be one of the two exchanges with a Spot trade client, and
        there is no leverage/margin/futures parameter to even pass."""
        if self._kill_switch_engaged:
            raise InventoryExecutionRefused(f"inventory kill switch engaged: {self._kill_switch_reason}")
        if not self._enabled:
            raise InventoryExecutionRefused("inventory_constitution_enabled is False")
        if sell_exchange not in ("binance", "bybit"):
            raise InventoryExecutionRefused(f"sell_exchange must be binance or bybit, got {sell_exchange!r}")
        if requested_usdt <= 0:
            raise InventoryExecutionRefused(f"requested_usdt must be positive, got {requested_usdt}")
        if requested_usdt > self._max_usdt_per_asset:
            raise InventoryExecutionRefused(
                f"requested {requested_usdt} USDT exceeds max_inventory_constitution_usdt_per_asset cap of {self._max_usdt_per_asset} USDT"
            )
        if self._in_flight_count >= self._max_concurrent_operations:
            raise InventoryExecutionRefused(f"max_concurrent_inventory_operations ({self._max_concurrent_operations}) already in flight")

    def register_operation_start(self) -> None:
        self._in_flight_count += 1

    def register_operation_end(self) -> None:
        self._in_flight_count = max(0, self._in_flight_count - 1)

    def status(self) -> dict:
        return {
            "inventory_constitution_enabled": self._enabled,
            "max_usdt_per_asset": self._max_usdt_per_asset,
            "max_concurrent_operations": self._max_concurrent_operations,
            "in_flight_count": self._in_flight_count,
            "kill_switch_engaged": self._kill_switch_engaged,
            "kill_switch_reason": self._kill_switch_reason,
            "kill_switch_at": self._kill_switch_at,
        }


def _build_default_inventory_guard() -> InventoryConstitutionGuard:
    from app.config.settings import get_settings

    settings = get_settings()
    return InventoryConstitutionGuard(
        inventory_constitution_enabled=settings.inventory_constitution_enabled,
        max_usdt_per_asset=settings.max_inventory_constitution_usdt_per_asset,
        max_concurrent_operations=settings.max_concurrent_inventory_operations,
    )


# Module-level singleton — same convention as app.execution.live_guard.live_guard.
inventory_guard = _build_default_inventory_guard()
