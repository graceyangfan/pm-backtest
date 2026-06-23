"""
Polymarket Fee Model for use with Nautilus Trader backtests using pmdata.dev data
(for up/down markets etc.).

Based on official docs https://docs.polymarket.com/trading/fees
and the reference implementation.

Fee formula (Crypto up/down: feeRate=0.07, maker=0):
    fee = C × feeRate × p × (1 - p)

Use in BacktestVenueConfig.fee_model = PolymarketFeeModel()

See comments in nautilus_pmdata_ingest.py for instrument setup (taker_fee=Decimal("0.07"))
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from nautilus_trader.backtest.models import FeeModel
from nautilus_trader.model.objects import Money


def basis_points_as_decimal(basis_points: Decimal) -> Decimal:
    """Convert basis points to decimal fraction."""
    return basis_points / Decimal(10_000)


def calculate_commission(
    quantity: Decimal,
    price: Decimal,
    fee_rate_bps: Decimal,
    fee_exponent: int = 1,
    **_kwargs: object,
) -> float:
    """
    Polymarket fee formula:
        fee = C * feeRate * p * (1 - p)
    """
    if fee_rate_bps <= 0:
        return 0.0
    del fee_exponent
    fee_rate = basis_points_as_decimal(fee_rate_bps)
    commission = quantity * fee_rate * price * (Decimal(1) - price)
    return float(commission.quantize(Decimal("0.00001"), rounding=ROUND_HALF_UP))


class PolymarketFeeModel(FeeModel):
    """
    Custom FeeModel for Polymarket that applies the quadratic fee formula.

    Set on your instrument: taker_fee as the decimal rate (e.g. Decimal("0.07") for your 0.07 rate).
    Maker is always 0.

    In backtest venue config:
        fee_model = PolymarketFeeModel()
    """

    def get_commission(self, order, fill_qty, fill_px, instrument) -> Money:
        taker_fee_dec = instrument.taker_fee
        fee_rate_bps = taker_fee_dec * Decimal(10_000)

        if fee_rate_bps <= 0:
            return Money(Decimal(0), instrument.quote_currency)

        commission = calculate_commission(
            quantity=Decimal(str(fill_qty)),
            price=Decimal(str(fill_px)),
            fee_rate_bps=fee_rate_bps,
        )
        return Money(Decimal(str(commission)), instrument.quote_currency)
