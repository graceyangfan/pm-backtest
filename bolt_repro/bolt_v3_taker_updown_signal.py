"""
Port of bolt-v2/src/bolt_v3_taker_updown_signal.rs plus the up/down
family fair-probability helper from bolt_v3_market_families/updown.rs.

This module is intentionally strict about fail-closed behavior so our replay
math stays aligned with the Rust strategy:
- expired markets return `None` for fair probability
- invalid probabilities fail closed
- zero effective volatility uses the deterministic expiry limit
"""

from dataclasses import dataclass
from typing import Optional
import math

# Constants from bolt_v3_numeric.rs
UNIT_F64 = 1.0
ZERO_F64 = 0.0
BPS_DENOMINATOR = 10000.0
POWER_OF_TWO = 2

def is_positive_finite(x: float) -> bool:
    return math.isfinite(x) and x > 0.0

def is_non_negative_finite(x: float) -> bool:
    return math.isfinite(x) and x >= 0.0

def clamp_probability(x: float) -> float:
    if not math.isfinite(x):
        return 0.0
    return max(0.0, min(1.0, x))

def sanitize_probability(x: float) -> Optional[float]:
    if not math.isfinite(x):
        return None
    if not (0.0 <= x <= 1.0):
        return None
    return x

def price_agreement_corr(observed_price: float, anchor_price: float) -> Optional[float]:
    if not is_positive_finite(observed_price) or not is_positive_finite(anchor_price):
        return None
    diff = abs(observed_price - anchor_price) / anchor_price
    return clamp_probability(UNIT_F64 - diff)

def price_gap_probability(observed_price: float, reference_price: float) -> Optional[float]:
    if not is_positive_finite(observed_price) or not is_positive_finite(reference_price):
        return None
    diff = abs(observed_price - reference_price) / reference_price
    return clamp_probability(diff)

@dataclass
class UncertaintyBandInputs:
    lead_gap_probability: float
    jitter_penalty_probability: float
    time_uncertainty_probability: float
    fee_uncertainty_probability: float

def uncertainty_band_probability(inputs: UncertaintyBandInputs) -> Optional[float]:
    p1 = sanitize_probability(inputs.lead_gap_probability)
    p2 = sanitize_probability(inputs.jitter_penalty_probability)
    p3 = sanitize_probability(inputs.time_uncertainty_probability)
    p4 = sanitize_probability(inputs.fee_uncertainty_probability)
    if None in (p1, p2, p3, p4):
        return None
    total = p1 + p2 + p3 + p4
    return sanitize_probability(total)

@dataclass
class ThetaScalerInputs:
    seconds_to_market_end: int
    cadence_seconds: int
    theta_decay_factor: float

def compute_theta_scaler(inputs: ThetaScalerInputs) -> Optional[float]:
    if not is_non_negative_finite(inputs.theta_decay_factor):
        return None
    if inputs.theta_decay_factor == ZERO_F64:
        return UNIT_F64
    if inputs.cadence_seconds == 0:
        return None
    ratio = clamp_probability(inputs.seconds_to_market_end / inputs.cadence_seconds)
    decay = UNIT_F64 - ratio
    return UNIT_F64 + inputs.theta_decay_factor * (decay ** POWER_OF_TWO)

class OutcomeSide:
    Up = "Up"
    Down = "Down"

def outcome_side_evidence_label(side: OutcomeSide) -> str:
    if isinstance(side, str):
        return side.lower()
    return side.value.lower() if hasattr(side, 'value') else str(side).lower()

@dataclass
class WorstCaseEvInputs:
    fair_probability: Optional[float]
    uncertainty_band_probability: float
    executable_entry_cost: float
    fee_bps: Optional[float]

def compute_worst_case_ev_bps(side: str, inputs: WorstCaseEvInputs) -> Optional[float]:
    fair_probability = sanitize_probability(inputs.fair_probability) if inputs.fair_probability is not None else None
    if fair_probability is None:
        return None
    uncertainty_band_probability = sanitize_probability(inputs.uncertainty_band_probability)
    if uncertainty_band_probability is None:
        return None
    executable_entry_cost = inputs.executable_entry_cost
    fee_bps = inputs.fee_bps
    if fee_bps is None or not is_non_negative_finite(fee_bps):
        return None
    if not is_positive_finite(executable_entry_cost):
        return None

    p_lo = clamp_probability(fair_probability - uncertainty_band_probability)
    p_hi = clamp_probability(fair_probability + uncertainty_band_probability)

    if side.lower() == "up":
        worst_case_success_probability = p_lo
    else:  # Down
        worst_case_success_probability = UNIT_F64 - p_hi

    total_entry_cost = executable_entry_cost * (UNIT_F64 + fee_bps / BPS_DENOMINATOR)

    if total_entry_cost <= ZERO_F64:
        return None

    ev = ((worst_case_success_probability - total_entry_cost) / total_entry_cost) * BPS_DENOMINATOR
    return ev

@dataclass
class SideSelectionInputs:
    up_worst_ev_bps: Optional[float]
    down_worst_ev_bps: Optional[float]
    min_worst_case_ev_bps: float

def choose_entry_side(inputs: SideSelectionInputs) -> Optional[str]:
    if not math.isfinite(inputs.min_worst_case_ev_bps):
        return None

    up = inputs.up_worst_ev_bps if (inputs.up_worst_ev_bps is not None and math.isfinite(inputs.up_worst_ev_bps)) else None
    down = inputs.down_worst_ev_bps if (inputs.down_worst_ev_bps is not None and math.isfinite(inputs.down_worst_ev_bps)) else None

    up_clears = up is not None and up > inputs.min_worst_case_ev_bps
    down_clears = down is not None and down > inputs.min_worst_case_ev_bps

    if up_clears and not down_clears:
        return "Up"
    if down_clears and not up_clears:
        return "Down"
    if up_clears and down_clears:
        if up is not None and down is not None:
            if up > down:
                return "Up"
            if down > up:
                return "Down"
        return None
    return None

# Minimal fair probability for updown (ported logic from
# bolt-v2 market_families/updown fair_probability_up).
SECONDS_PER_YEAR_F64 = 365.25 * 24 * 3600
SIGMA_SQUARED_HALF_DIVISOR = 2.0
KURTOSIS_NORMALIZATION = 6.0
NORMAL_DENSITY_EXPONENT_DIVISOR = 2.0
NORMAL_CDF_T_SCALE = 0.2316419
NORMAL_CDF_DENSITY_SCALE = 0.3989423
NORMAL_CDF_POLY_A1 = 0.3193815
NORMAL_CDF_POLY_A2 = -0.3565638
NORMAL_CDF_POLY_A3 = 1.781478
NORMAL_CDF_POLY_A4 = -1.821256
NORMAL_CDF_POLY_A5 = 1.330274

def standard_normal_cdf(x: float) -> float:
    if not math.isfinite(x):
        return 0.5
    t = 1.0 / (1.0 + NORMAL_CDF_T_SCALE * abs(x))
    d = NORMAL_CDF_DENSITY_SCALE * math.exp(-x * x / NORMAL_DENSITY_EXPONENT_DIVISOR)
    poly = NORMAL_CDF_POLY_A1 + t * (NORMAL_CDF_POLY_A2 + t * (NORMAL_CDF_POLY_A3 + t * (NORMAL_CDF_POLY_A4 + t * NORMAL_CDF_POLY_A5)))
    prob = d * t * poly
    return 1.0 - prob if x > 0.0 else prob


def deterministic_up_probability(spot: float, strike: float) -> float:
    if spot > strike:
        return 1.0
    if spot < strike:
        return 0.0
    return 0.5


def fair_probability_up(spot: float, strike: float, seconds_to_expiry: float, realized_vol: float = 0.9, kurtosis: float = 0.0) -> Optional[float]:
    """Core model fair P(spot_end > strike) using the aligned Rust formula."""
    if not is_positive_finite(spot) or not is_positive_finite(strike):
        return None
    if not is_non_negative_finite(realized_vol):
        return None
    if not math.isfinite(kurtosis):
        return None

    t_years = seconds_to_expiry / SECONDS_PER_YEAR_F64
    if t_years <= 0.0:
        return None

    sigma_eff = realized_vol * (1.0 + kurtosis / KURTOSIS_NORMALIZATION)
    if not is_non_negative_finite(sigma_eff):
        return None
    if sigma_eff == 0.0:
        return deterministic_up_probability(spot, strike)

    try:
        d2 = (math.log(spot / strike) - (sigma_eff ** 2 / SIGMA_SQUARED_HALF_DIVISOR) * t_years) / (sigma_eff * math.sqrt(t_years))
        p = standard_normal_cdf(d2)
        return clamp_probability(p)
    except Exception:
        return None
    side2 = choose_entry_side(SideSelectionInputs(8.0, 7.0, 8.0))
    assert side2 is None
    side3 = choose_entry_side(SideSelectionInputs(9.0, None, 8.0))
    assert side3 == "Up"
    side4 = choose_entry_side(SideSelectionInputs(float('nan'), 9.0, 8.0))
    assert side4 == "Down"
    side5 = choose_entry_side(SideSelectionInputs(float('nan'), None, 8.0))
    assert side5 is None
    side6 = choose_entry_side(SideSelectionInputs(9.0, 9.0, 8.0))
    assert side6 is None
    print("side_selection tests passed")

    print("All consistency tests PASSED")
