"""The venue's own product catalogue, resolved once at startup.

Sizes, ticks, fee rates, margin percentages and funding intervals differ per
instrument - one DOGEUSD contract is 100 DOGE, one BTCUSD contract is 0.001
BTC, BTCUSD allows 200x and ADAUSD 100x. Nothing in the console hardcodes any
of it: everything reads what the exchange reports, so a fee change or a
contract-size change is picked up rather than silently mispriced.

Margin, as Delta publishes it per product:

* `initial_margin` / `maintenance_margin` are *percentages* (BTCUSD 0.5 and
  0.25, i.e. 200x and a 0.25% maintenance requirement).
* Up to `max_leverage_notional` USD of position those minimums apply. Above it
  the requirement rises linearly: `IM% = initial_margin +
  initial_margin_scaling_factor * (notional - max_leverage_notional)`, and the
  same shape for maintenance. The factor is per USD of notional, not per
  contract: it is identical on ADAUSD ($0.20 a contract) and SOLUSD ($100 a
  contract), which only a notional-based slope can be.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

log = logging.getLogger("instruments")

TRADABLE_STATES = {"live"}
TRADABLE_STATUS = {"operational"}


@dataclass(frozen=True)
class Instrument:
    symbol: str
    product_id: int
    tick_size: Decimal
    contract_value: Decimal      # underlying units per contract
    taker_fee: float             # fraction of notional, before GST
    maker_fee: float
    max_leverage: float
    initial_margin_pct: float = 1.0
    maintenance_margin_pct: float = 0.5
    im_scaling: float = 0.0      # percentage points per USD above the threshold
    mm_scaling: float = 0.0
    max_leverage_notional: float = math.inf
    position_size_limit: int = 0         # contracts; 0 = no limit reported
    position_notional_limit: float = 0.0  # USD; 0 = no limit reported
    funding_interval: int = 28800        # seconds between funding exchanges
    description: str = ""

    def round_price(self, price: float) -> str:
        """Snap to the instrument's tick, as a string the API accepts verbatim."""
        quantum = self.tick_size
        value = (Decimal(str(price)) / quantum).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        ) * quantum
        return format(value.quantize(quantum), "f")

    def contracts_for(self, underlying_units: float) -> int:
        """Whole contracts covering the requested exposure, rounded down."""
        if underlying_units <= 0:
            return 0
        return int(
            (Decimal(str(underlying_units)) / self.contract_value).to_integral_value(
                rounding=ROUND_DOWN
            )
        )

    def notional(self, contracts: int, price: float) -> float:
        return float(self.contract_value) * abs(contracts) * price

    # -- margin ----------------------------------------------------------
    def _excess(self, notional: float) -> float:
        return max(0.0, abs(notional) - self.max_leverage_notional)

    def im_fraction(self, notional: float) -> float:
        """Minimum initial margin, as a fraction of notional, for a position this big."""
        return min(1.0, (self.initial_margin_pct + self.im_scaling * self._excess(notional)) / 100.0)

    def mm_fraction(self, notional: float) -> float:
        """Maintenance margin, as a fraction of notional, for a position this big."""
        return min(1.0, (self.maintenance_margin_pct + self.mm_scaling * self._excess(notional)) / 100.0)

    def leverage_cap(self, notional: float = 0.0) -> float:
        """Highest leverage the venue allows on a position of this notional."""
        return max(1.0, min(self.max_leverage, 1.0 / self.im_fraction(notional)))

    def spec(self) -> dict:
        return {
            "symbol": self.symbol,
            "product_id": self.product_id,
            "description": self.description,
            "tick_size": str(self.tick_size),
            "contract_value": str(self.contract_value),
            "taker_fee": self.taker_fee,
            "maker_fee": self.maker_fee,
            "max_leverage": self.max_leverage,
            "initial_margin_pct": self.initial_margin_pct,
            "maintenance_margin_pct": self.maintenance_margin_pct,
            "max_leverage_notional": (None if math.isinf(self.max_leverage_notional)
                                      else self.max_leverage_notional),
            "position_size_limit": self.position_size_limit,
            "position_notional_limit": self.position_notional_limit,
            "funding_interval": self.funding_interval,
        }


def _decimal(value, default: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
        return parsed if parsed > 0 else Decimal(default)
    except Exception:
        return Decimal(default)


def _float(value, default: float) -> float:
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else default
    except (TypeError, ValueError):
        return default


def _leverage(row) -> float:
    """Leverage the venue allows on this product at its minimum margin.

    Delta reports `initial_margin` as a *percentage* - ADAUSD is `1`, meaning
    1% and therefore 100x, which is exactly the `default_leverage` it also
    reports.
    """
    initial_margin_percent = _float(row.get("initial_margin"), 0.0)
    if initial_margin_percent > 0:
        return 100.0 / initial_margin_percent
    leverage = _float(row.get("default_leverage"), 0.0)
    return leverage if leverage > 0 else 1.0


def from_row(row: dict) -> Instrument:
    specs = row.get("product_specs") or {}
    leverage = _leverage(row)
    return Instrument(
        symbol=str(row["symbol"]).upper(),
        product_id=int(row["id"]),
        tick_size=_decimal(row.get("tick_size"), "0.0001"),
        contract_value=_decimal(row.get("contract_value"), "1"),
        taker_fee=_float(row.get("taker_commission_rate"), 0.0005),
        maker_fee=_float(row.get("maker_commission_rate"), 0.0002),
        max_leverage=leverage,
        initial_margin_pct=_float(row.get("initial_margin"), 100.0 / leverage),
        maintenance_margin_pct=_float(row.get("maintenance_margin"), 50.0 / leverage),
        im_scaling=_float(row.get("initial_margin_scaling_factor"), 0.0),
        mm_scaling=_float(row.get("maintenance_margin_scaling_factor"), 0.0),
        max_leverage_notional=_float(row.get("max_leverage_notional"), math.inf) or math.inf,
        position_size_limit=int(_float(row.get("position_size_limit"), 0)),
        position_notional_limit=_float(row.get("position_notional_limit"), 0.0),
        funding_interval=int(_float(specs.get("rate_exchange_interval"), 28800)) or 28800,
        description=str(row.get("description") or row.get("short_description") or ""),
    )


def load(client, symbols) -> dict[str, Instrument]:
    """Resolve `symbols` against the venue, dropping what it will not trade."""
    wanted = {s.upper() for s in symbols}
    rows = client.get(
        "/v2/products", {"page_size": 1000, "contract_types": "perpetual_futures"},
        signed=False,
    )
    rows = rows if isinstance(rows, list) else []
    if len(rows) >= 1000:
        log.warning("product catalogue filled a full page; some symbols may be missing")
    catalogue = {
        str(row.get("symbol", "")).upper(): row
        for row in rows
        if str(row.get("symbol", "")).upper() in wanted
    }

    resolved: dict[str, Instrument] = {}
    unavailable: list[str] = []
    for symbol in symbols:
        row = catalogue.get(symbol.upper())
        if row is None:
            unavailable.append(f"{symbol} (not listed)")
            continue
        state = str(row.get("state", "")).lower()
        status = str(row.get("trading_status", "")).lower()
        if state not in TRADABLE_STATES or status not in TRADABLE_STATUS:
            unavailable.append(f"{symbol} ({state}/{status})")
            continue
        resolved[symbol.upper()] = from_row(row)

    if unavailable:
        log.warning("%d of %d configured instruments are not tradable and were "
                    "skipped: %s", len(unavailable), len(symbols), ", ".join(unavailable))
    if not resolved:
        raise RuntimeError("None of the configured instruments are tradable; nothing to trade.")
    log.info("Trading %d instruments: %s", len(resolved), ", ".join(sorted(resolved)))
    return resolved


if __name__ == "__main__":
    inst = Instrument("X", 1, Decimal("0.00001"), Decimal("100"), 0.0005, 0.0002, 100)
    assert inst.round_price(0.211043) == "0.21104"
    assert inst.round_price(0.2110451) == "0.21105"
    assert inst.contracts_for(250) == 2
    assert inst.contracts_for(50) == 0
    assert abs(inst.notional(2, 0.2) - 40.0) < 1e-9

    assert _leverage({"default_leverage": "100", "initial_margin": "1"}) == 100.0
    assert _leverage({"initial_margin": "0.5"}) == 200.0
    assert _leverage({}) == 1.0

    # BTCUSD as the venue published it on 2026-10-24.
    btc = from_row({
        "symbol": "BTCUSD", "id": 27, "tick_size": "0.5", "contract_value": "0.001",
        "taker_commission_rate": "0.0005", "maker_commission_rate": "0.0002",
        "initial_margin": "0.5", "maintenance_margin": "0.25", "default_leverage": "200",
        "initial_margin_scaling_factor": "0.0000025",
        "maintenance_margin_scaling_factor": "0.00000125",
        "max_leverage_notional": "100000", "position_size_limit": 125000,
        "position_notional_limit": "5000000",
        "product_specs": {"rate_exchange_interval": 28800},
    })
    assert btc.max_leverage == 200 and btc.funding_interval == 28800
    assert abs(btc.im_fraction(50_000) - 0.005) < 1e-12
    assert abs(btc.mm_fraction(50_000) - 0.0025) < 1e-12
    # $300k: 0.5% + 0.0000025 * 200000 = 1.0% -> 100x.
    assert abs(btc.im_fraction(300_000) - 0.01) < 1e-12
    assert abs(btc.leverage_cap(300_000) - 100.0) < 1e-9
    assert abs(btc.mm_fraction(300_000) - 0.005) < 1e-12
    print("instruments self-check ok")
