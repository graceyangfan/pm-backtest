"""
Port of key parts from bolt_v3_binary_outcome_edge.rs and related.

For consistency with the Rust implementation of BinaryOutcomeEdge evaluation.
"""

from dataclasses import dataclass
from typing import Optional
from enum import Enum
import math

from bolt_repro.bolt_v3_numeric import (
    UNIT_F64, ZERO_F64, BPS_DENOMINATOR, CENTS_PER_SHARE,
    is_non_negative_finite, is_positive_finite, sanitize_probability
)
from bolt_repro.bolt_v3_taker_updown_signal import OutcomeSide
from bolt_repro.bolt_v3_executable_cost import ExecutableCostBreakdown

# Simplified from Rust
class BinaryOutcomeEdgeBlockReason(Enum):
    MissingOrderBook = "missing_order_book"
    InsufficientDepth = "insufficient_depth"
    FeeUnavailable = "fee_unavailable"
    InvalidProbability = "invalid_probability"
    InvalidCost = "invalid_cost"
    UnsupportedOrderShape = "unsupported_order_shape"
    EdgeBelowThreshold = "edge_below_threshold"
    SpreadOrSlippageWipedEdge = "spread_or_slippage_wiped_edge"

@dataclass
class BinaryOutcomeEdgeInputs:
    side: str  # "Up" or "Down"
    fair_probability_up: Optional[float]
    adjusted_probability_up: Optional[float]
    order_side: str  # "Buy" etc.
    cost_breakdown: ExecutableCostBreakdown
    minimum_edge_bps: float

@dataclass
class BinaryOutcomeEdgeResult:
    selected_side: str
    adjusted_probability: float
    edge_bps: float
    edge_cents_per_share: float
    cost_breakdown: ExecutableCostBreakdown
    trade_allowed: bool
    block_reason: Optional[BinaryOutcomeEdgeBlockReason] = None

    @classmethod
    def blocked(cls, side: str, cost_breakdown: ExecutableCostBreakdown, reason: BinaryOutcomeEdgeBlockReason):
        return cls(
            selected_side=side,
            adjusted_probability=0.0,
            edge_bps=0.0,
            edge_cents_per_share=0.0,
            cost_breakdown=cost_breakdown,
            trade_allowed=False,
            block_reason=reason
        )

    @classmethod
    def blocked_with_cost(cls, side: str, cost_breakdown: ExecutableCostBreakdown, reason: BinaryOutcomeEdgeBlockReason):
        return cls.blocked(side, cost_breakdown, reason)

def evaluate_binary_outcome_edge(inputs: BinaryOutcomeEdgeInputs) -> BinaryOutcomeEdgeResult:
    if inputs.order_side.lower() != "buy":
        return BinaryOutcomeEdgeResult.blocked_with_cost(
            inputs.side,
            inputs.cost_breakdown,
            BinaryOutcomeEdgeBlockReason.UnsupportedOrderShape
        )

    adjusted_up = sanitize_probability(inputs.adjusted_probability_up)
    if adjusted_up is None:
        return BinaryOutcomeEdgeResult.blocked_with_cost(
            inputs.side,
            inputs.cost_breakdown,
            BinaryOutcomeEdgeBlockReason.InvalidProbability
        )

    fair_up = sanitize_probability(inputs.fair_probability_up)
    if fair_up is None:
        return BinaryOutcomeEdgeResult.blocked_with_cost(
            inputs.side,
            inputs.cost_breakdown,
            BinaryOutcomeEdgeBlockReason.InvalidProbability
        )

    if not inputs.cost_breakdown.cost_available:
        reason = inputs.cost_breakdown.block_reason or "InvalidCost"
        br = next((r for r in BinaryOutcomeEdgeBlockReason if r.value == reason), BinaryOutcomeEdgeBlockReason.InvalidCost)
        return BinaryOutcomeEdgeResult.blocked_with_cost(inputs.side, inputs.cost_breakdown, br)

    success_probability = adjusted_up if inputs.side.lower() == "up" else (UNIT_F64 - adjusted_up)
    fair_success_probability = fair_up if inputs.side.lower() == "up" else (UNIT_F64 - fair_up)

    if not is_non_negative_finite(success_probability) or success_probability > UNIT_F64:
        return BinaryOutcomeEdgeResult.blocked_with_cost(
            inputs.side,
            inputs.cost_breakdown,
            BinaryOutcomeEdgeBlockReason.InvalidProbability
        )

    gross_edge_cents_per_share = fair_success_probability * CENTS_PER_SHARE - inputs.cost_breakdown.gross_cost_cents
    edge_cents_per_share = success_probability * CENTS_PER_SHARE - inputs.cost_breakdown.total_adjusted_cost_cents

    if not is_positive_finite(inputs.cost_breakdown.total_adjusted_cost_cents):
        return BinaryOutcomeEdgeResult.blocked_with_cost(
            inputs.side,
            inputs.cost_breakdown,
            BinaryOutcomeEdgeBlockReason.InvalidCost
        )

    edge_bps = (edge_cents_per_share / inputs.cost_breakdown.total_adjusted_cost_cents) * BPS_DENOMINATOR

    if not math.isfinite(edge_bps) or not math.isfinite(edge_cents_per_share):
        return BinaryOutcomeEdgeResult.blocked_with_cost(
            inputs.side,
            inputs.cost_breakdown,
            BinaryOutcomeEdgeBlockReason.InvalidCost
        )

    block_reason = None
    if edge_cents_per_share <= ZERO_F64 or not math.isfinite(inputs.minimum_edge_bps) or edge_bps <= inputs.minimum_edge_bps:
        if gross_edge_cents_per_share > ZERO_F64 and edge_cents_per_share <= ZERO_F64:
            block_reason = BinaryOutcomeEdgeBlockReason.SpreadOrSlippageWipedEdge
        else:
            block_reason = BinaryOutcomeEdgeBlockReason.EdgeBelowThreshold

    return BinaryOutcomeEdgeResult(
        selected_side=inputs.side,
        adjusted_probability=success_probability,
        edge_bps=edge_bps,
        edge_cents_per_share=edge_cents_per_share,
        cost_breakdown=inputs.cost_breakdown,
        trade_allowed=block_reason is None,
        block_reason=block_reason
    )

# Add more as needed from the file

# Test cases ported from Rust tests in bolt_v3_binary_outcome_edge.rs for consistency
if __name__ == "__main__":
    print("Testing BinaryOutcomeEdge consistency...")

    def make_cost(vwap=0.50, fee_bps=0.0, slippage=0):
        gross = vwap * 100
        fee = gross * (fee_bps / 10000.0)
        slip = slippage / 100.0
        total = gross + fee + slip
        return ExecutableCostBreakdown(
            cost_available=True,
            gross_cost_cents=gross,
            total_adjusted_cost_cents=total,
            slippage_buffer_cents=slip
        )

    # up_and_down_use_adjusted_probability_from_precomputed_cost
    cost = make_cost(0.50, 0.0, 0)
    inp_up = BinaryOutcomeEdgeInputs("Up", 0.64, 0.64, "Buy", cost, 0.0)
    up = evaluate_binary_outcome_edge(inp_up)
    inp_down = BinaryOutcomeEdgeInputs("Down", 0.64, 0.64, "Buy", cost, 0.0)
    down = evaluate_binary_outcome_edge(inp_down)
    assert abs(up.adjusted_probability - 0.64) < 1e-9
    assert abs(up.edge_bps - 2800.0) < 1e-9
    assert up.trade_allowed
    assert abs(down.adjusted_probability - 0.36) < 1e-9
    assert abs(down.edge_bps + 2800.0) < 1e-9
    assert down.block_reason == BinaryOutcomeEdgeBlockReason.EdgeBelowThreshold
    print("up_and_down test passed")

    # fair_probability_controls_fee_slippage_wipe_classification
    cost = make_cost(0.50, 200.0, 0)
    inp = BinaryOutcomeEdgeInputs("Up", 0.51, 0.505, "Buy", cost, 0.0)
    res = evaluate_binary_outcome_edge(inp)
    assert res.block_reason == BinaryOutcomeEdgeBlockReason.SpreadOrSlippageWipedEdge
    assert not res.trade_allowed
    print("fee_slippage_wipe test passed")

    print("Edge consistency tests PASSED")

