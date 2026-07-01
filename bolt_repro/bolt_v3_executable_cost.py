"""
Port of bolt_v3_executable_cost.rs for consistency with bolt-v2.

Key functions:
- price_exact_size_vwap: computes VWAP for exact size within depth limit.
- consume_exact_notional_level
- executable_cost_breakdown: adds fee and slippage.

Integrates with OutcomeBookState.
Uses Polymarket crypto fee formula where applicable: 0.07 * qty * p * (1-p) but here we take fee_bps as input.
"""

from dataclasses import dataclass
from typing import Optional
from collections import OrderedDict
import math

from bolt_repro.bolt_v3_numeric import (
    BPS_DENOMINATOR, CENTS_PER_SHARE, UNIT_F64, ZERO_F64,
    is_non_negative_finite, is_positive_finite, notional_float_tolerance
)
from bolt_repro.bolt_v3_book_sizing import OutcomeBookState
from nautilus_trader.model.enums import OrderSide

@dataclass
class ExactSizeVwap:
    vwap_price: float
    vwap_quantity: float
    limit_price: float
    exact_size_filled: bool

@dataclass
class ExecutableCostBreakdown:
    vwap_price: Optional[float] = None
    vwap_quantity: Optional[float] = None
    limit_price: Optional[float] = None
    exact_size_filled: bool = False
    gross_cost_cents: float = 0.0
    fee_cost_cents: float = 0.0
    slippage_buffer_cents: float = 0.0
    total_adjusted_cost_cents: float = 0.0
    cost_available: bool = True
    block_reason: Optional[str] = None

    @classmethod
    def blocked(cls, reason: str):
        return cls(
            cost_available=False,
            block_reason=reason,
            gross_cost_cents=ZERO_F64,
            fee_cost_cents=ZERO_F64,
            slippage_buffer_cents=ZERO_F64,
            total_adjusted_cost_cents=ZERO_F64,
        )

def price_exact_size_vwap(
    book: OutcomeBookState,
    order_side: OrderSide,
    edge_pricing_notional: float,
    vwap_depth_limit_bps: int = 0,
) -> ExactSizeVwap:
    """
    Port of price_exact_size_vwap.
    Computes VWAP for exact notional within depth limit.
    """
    if not is_positive_finite(edge_pricing_notional):
        raise ValueError("InvalidCost")

    if order_side == OrderSide.BUY:
        best_touch = book.best_ask
        is_buy = True
        levels = sorted(book.ask_levels.items()) if hasattr(book, 'ask_levels') else []
    elif order_side == OrderSide.SELL:
        best_touch = book.best_bid
        is_buy = False
        levels = sorted(book.bid_levels.items(), reverse=True) if hasattr(book, 'bid_levels') else []
    else:
        raise ValueError("UnsupportedOrderShape")

    if best_touch is None or not is_positive_finite(best_touch):
        raise ValueError("MissingOrderBook")

    depth_limit = vwap_depth_limit_bps / BPS_DENOMINATOR
    allowed_vwap = best_touch * (UNIT_F64 + depth_limit) if is_buy else best_touch * (UNIT_F64 - depth_limit)

    if not is_positive_finite(allowed_vwap):
        raise ValueError("InvalidCost")

    remaining_notional = edge_pricing_notional
    filled_quantity = ZERO_F64
    filled_notional = ZERO_F64
    limit_price = None

    for price, size in levels:
        price_f = float(price) if not isinstance(price, float) else price
        if (is_buy and price_f > allowed_vwap) or (not is_buy and price_f < allowed_vwap):
            break
        previous_remaining = remaining_notional
        take_notional = min(remaining_notional, price_f * size)
        take_quantity = take_notional / price_f
        filled_quantity += take_quantity
        filled_notional += take_notional
        remaining_notional -= take_notional
        if remaining_notional < previous_remaining:
            limit_price = price_f
        if remaining_notional <= ZERO_F64:
            break

    if remaining_notional > notional_float_tolerance(edge_pricing_notional) or not is_positive_finite(filled_quantity):
        raise ValueError("InsufficientDepth")

    vwap_price = filled_notional / filled_quantity
    if not is_positive_finite(vwap_price):
        raise ValueError("InvalidCost")

    if limit_price is None or not is_positive_finite(limit_price):
        raise ValueError("InvalidCost")

    within_depth = (
        (vwap_price <= allowed_vwap and limit_price <= allowed_vwap) if is_buy else
        (vwap_price >= allowed_vwap and limit_price >= allowed_vwap)
    )
    if not within_depth:
        raise ValueError("InsufficientDepth")

    return ExactSizeVwap(
        vwap_price=vwap_price,
        vwap_quantity=filled_quantity,
        limit_price=limit_price,
        exact_size_filled=True
    )

def executable_cost_breakdown(
    vwap: ExactSizeVwap,
    fee_bps: float,
    slippage_buffer_bps: int = 0,
) -> ExecutableCostBreakdown:
    """
    Port of executable_cost_breakdown.
    Adds fee and slippage using the formula.
    For Polymarket crypto: fee part can be adjusted to 0.07 * qty * p * (1-p) externally if needed.
    """
    if not is_non_negative_finite(fee_bps):
        raise ValueError("FeeUnavailable")

    gross_cost_cents = vwap.vwap_price * CENTS_PER_SHARE
    fee_cost_cents = gross_cost_cents * fee_bps / BPS_DENOMINATOR
    slippage_buffer_cents = gross_cost_cents * slippage_buffer_bps / BPS_DENOMINATOR
    total_adjusted_cost_cents = gross_cost_cents + fee_cost_cents + slippage_buffer_cents

    if not is_positive_finite(gross_cost_cents) or not is_positive_finite(total_adjusted_cost_cents):
        raise ValueError("InvalidCost")

    return ExecutableCostBreakdown(
        vwap_price=vwap.vwap_price,
        vwap_quantity=vwap.vwap_quantity,
        limit_price=vwap.limit_price,
        exact_size_filled=vwap.exact_size_filled,
        gross_cost_cents=gross_cost_cents,
        fee_cost_cents=fee_cost_cents,
        slippage_buffer_cents=slippage_buffer_cents,
        total_adjusted_cost_cents=total_adjusted_cost_cents,
        cost_available=True,
    )

# Helper to build from OutcomeBookState (for strategy use)
def compute_executable_cost_from_book(
    book: OutcomeBookState,
    side: str,
    notional: float,
    fee_bps: float = 0.0,
    slippage_bps: int = 0,
    depth_limit_bps: int = 0,
) -> ExecutableCostBreakdown:
    try:
        order_side = OrderSide.BUY if side.lower() == "buy" or side == "Up" else OrderSide.SELL
        vwap = price_exact_size_vwap(book, order_side, notional, depth_limit_bps)
        return executable_cost_breakdown(vwap, fee_bps, slippage_bps)
    except Exception as e:
        reason = str(e) if str(e) in ["MissingOrderBook", "InsufficientDepth", "InvalidCost", "FeeUnavailable", "UnsupportedOrderShape"] else "InvalidCost"
        return ExecutableCostBreakdown.blocked(reason)
