"""FX rate provision (Core System Design v0.1, FIX-01).

Exposure and portfolio value must be aggregated in a single base currency. FX
rates are supplied through an explicit provider - the system never guesses a
rate. A missing or non-positive rate raises, and the Risk gate turns that into
``UNKNOWN`` (fail closed, P2).
"""

from __future__ import annotations

import math
from typing import Protocol, runtime_checkable

from qat.core.models import Currency


class FXError(RuntimeError):
    """Base class for FX-rate problems."""


class MissingFXRateError(FXError):
    def __init__(self, key) -> None:
        super().__init__(f"no FX rate configured for {key}")
        self.key = key


class InvalidFXRateError(FXError):
    def __init__(self, key) -> None:
        super().__init__(f"FX rate for {key} is not a finite positive number")
        self.key = key


@runtime_checkable
class FXRateProvider(Protocol):
    def rate(self, from_currency: Currency, to_currency: Currency) -> float: ...


class StaticFXRateProvider:
    """Deterministic provider for tests and paper trading.

    ``rates`` maps ``(from_currency, to_currency)`` to a positive multiplier such
    that ``amount_in_to = amount_in_from * rate``. Same-currency conversions are
    always ``1.0``. Anything else not in the table raises ``MissingFXRateError``.
    """

    def __init__(self, rates: dict | None = None) -> None:
        self._rates: dict[tuple[Currency, Currency], float] = {}
        for (frm, to), value in dict(rates or {}).items():
            self._rates[(Currency(frm), Currency(to))] = float(value)

    def rate(self, from_currency, to_currency) -> float:
        frm = Currency(from_currency)
        to = Currency(to_currency)
        if frm == to:
            return 1.0
        key = (frm, to)
        if key not in self._rates:
            raise MissingFXRateError(key)
        value = self._rates[key]
        if not math.isfinite(value) or value <= 0:
            raise InvalidFXRateError(key)
        return value
