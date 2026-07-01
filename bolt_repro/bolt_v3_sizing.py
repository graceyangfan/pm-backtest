"""
Port of bolt-v2/src/bolt_v3_sizing.rs.
"""

from dataclasses import dataclass

from bolt_repro.bolt_v3_numeric import ZERO_F64, is_non_negative_finite, is_positive_finite

QUADRATIC_RISK_DIVISOR = 2.0


def sanitize_non_negative(value: float) -> float:
    return value if is_non_negative_finite(value) else ZERO_F64


@dataclass
class RobustSizingInputs:
    expected_ev_per_notional: float
    ev_reference_per_notional: float
    risk_lambda: float
    order_notional_target: float
    maximum_position_notional: float
    impact_cap_notional: float


def choose_robust_size(inputs: RobustSizingInputs) -> float:
    if not is_positive_finite(inputs.expected_ev_per_notional):
        return ZERO_F64

    cap = min(
        sanitize_non_negative(inputs.order_notional_target),
        sanitize_non_negative(inputs.maximum_position_notional),
        sanitize_non_negative(inputs.impact_cap_notional),
    )
    if cap <= ZERO_F64:
        return ZERO_F64

    if not is_non_negative_finite(inputs.risk_lambda):
        return ZERO_F64
    if not is_positive_finite(inputs.ev_reference_per_notional):
        return ZERO_F64
    if inputs.risk_lambda == ZERO_F64:
        return cap

    target_scale = max(
        0.0,
        min(
            1.0,
            inputs.expected_ev_per_notional
            / (
                QUADRATIC_RISK_DIVISOR
                * inputs.risk_lambda
                * inputs.ev_reference_per_notional
            ),
        ),
    )
    return min(sanitize_non_negative(inputs.order_notional_target) * target_scale, cap)
