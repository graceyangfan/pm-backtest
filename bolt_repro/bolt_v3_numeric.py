"""
Port of constants and helpers from bolt_v3_numeric.rs for exact consistency.
"""

UNIT_F64 = 1.0
ZERO_F64 = 0.0
HALF_F64 = 0.5
BPS_DENOMINATOR = 10000.0
CENTS_PER_SHARE = 100.0
POWER_OF_TWO = 2

def is_positive_finite(x: float) -> bool:
    import math
    return math.isfinite(x) and x > 0.0

def is_non_negative_finite(x: float) -> bool:
    import math
    return math.isfinite(x) and x >= 0.0

def clamp_probability(x: float) -> float:
    import math
    if not math.isfinite(x):
        return 0.0
    return max(0.0, min(1.0, x))

def sanitize_probability(x: float) -> float | None:
    import math
    if not math.isfinite(x):
        return None
    if not (0.0 <= x <= 1.0):
        return None
    return x

def notional_float_tolerance(notional: float) -> float:
    return 1e-9 * max(1.0, notional)  # simple tolerance
