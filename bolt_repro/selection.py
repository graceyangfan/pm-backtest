"""
Port of key selection logic from bolt-v2/src/strategies/binary_oracle_edge_taker/selection.rs
and bolt_v3_market_families/updown.rs for consistency.

This handles market selection/rotation for updown (5m/15m) events.
"""

from dataclasses import dataclass
from typing import List, Optional, Dict, Any
from nautilus_trader.model.identifiers import InstrumentId

TARGET_MARKET_NOT_FOUND_REASON = "target_market_not_found"

@dataclass
class CandidateOutcome:
    instrument_id: str

@dataclass
class CandidateMarket:
    market_id: str
    instrument_id: str
    up: CandidateOutcome
    down: CandidateOutcome
    source_identity: Any
    selection_outcome: Any
    price_to_beat: Optional[float] = None
    start_ts_ms: int = 0
    expiration_ts_ms: int = 0
    seconds_to_end: int = 0

@dataclass
class SelectionDecision:
    ruleset_id: str
    state: Any  # Active {market}, Idle {reason}, (Freeze in tests)

@dataclass
class SelectionState:
    Active = "Active"
    Freeze = "Freeze"
    Idle = "Idle"

@dataclass
class RuntimeSelectionSnapshot:
    ruleset_id: str
    decision: SelectionDecision
    eligible_candidates: List[CandidateMarket]
    published_at_ms: int

def idle_selection_snapshot(config_or_ruleset: Any, now_ms: int, reason: str) -> RuntimeSelectionSnapshot:
    ruleset = getattr(config_or_ruleset, "ruleset_id", "updown") if not isinstance(config_or_ruleset, str) else config_or_ruleset
    return RuntimeSelectionSnapshot(
        ruleset_id=ruleset,
        decision=SelectionDecision(ruleset_id=ruleset, state=type("Idle", (), {"reason": reason})()),
        eligible_candidates=[],
        published_at_ms=now_ms,
    )

# Simplified for our fixed target set (no full config targets/rotation)
def select_current_market(slugs: List[str], now_ms: int, config_targets: Dict) -> Optional[CandidateMarket]:
    """Simplified selection for updown.
    In bolt-v2, it uses config targets, time windows for 5m/15m rotation.
    For our data, pick based on slug timestamp.
    """
    for slug in slugs:
        try:
            ts = int(slug.split('-')[-1])
            ts_ms = ts * 1000
            if abs(ts_ms - now_ms) < 3600 * 1000:  # within hour of interest
                iid = slug + ".POLYMARKET"
                return CandidateMarket(
                    market_id=slug,
                    instrument_id=iid,
                    up=CandidateOutcome(instrument_id=iid),
                    down=CandidateOutcome(instrument_id=iid),
                    source_identity=None,
                    selection_outcome="Current",
                    start_ts_ms=ts_ms,
                    expiration_ts_ms=ts_ms + 300*1000,
                    seconds_to_end=300
                )
        except Exception:
            pass
    return None

def apply_selection_snapshot_to_active(active: "ActiveMarketState", snapshot: RuntimeSelectionSnapshot, warmup_target: int = 0):
    """Port of apply_selection_snapshot_to_active from selection.rs"""
    from bolt_repro.models import ActiveMarketState  # local
    previous_books = getattr(active, "books", None)
    previous_trade_flow = getattr(active, "trade_flow", {}) or {}
    next_state = ActiveMarketState.from_snapshot(snapshot, warmup_target)
    preserve_books = bool(active.market_id) and active.market_id == next_state.market_id and active.instrument_id == next_state.instrument_id
    if active.same_boundary(next_state):
        active.trade_flow = previous_trade_flow
        return
    # simple same market transition check
    if (active.market_id and active.market_id == next_state.market_id and active.instrument_id == next_state.instrument_id
        and getattr(active, "interval_start_ms", None) == getattr(next_state, "interval_start_ms", None)):
        active.phase = next_state.phase
        active.forced_flat = next_state.forced_flat
        active.market_selection_outcome = next_state.market_selection_outcome
        active.interval_end_ms = next_state.interval_end_ms
        active.trade_flow = previous_trade_flow
        return
    # full replace
    for k, v in next_state.__dict__.items():
        setattr(active, k, v)
    active.trade_flow = previous_trade_flow
    if preserve_books and previous_books is not None:
        active.books = previous_books

def same_market_transition(current: "ActiveMarketState", next_s: "ActiveMarketState") -> bool:
    return bool(current.market_id) and current.market_id == next_s.market_id and current.instrument_id == next_s.instrument_id \
        and getattr(current, "market_selection_outcome", None) == getattr(next_s, "market_selection_outcome", None) \
        and getattr(current, "interval_start_ms", None) == getattr(next_s, "interval_start_ms", None) \
        and getattr(current, "interval_end_ms", None) == getattr(next_s, "interval_end_ms", None)

def selection_snapshot_from_instruments(instruments: List[str], now_ms: int) -> RuntimeSelectionSnapshot:
    """Build a snapshot selecting our target if present."""
    cands = []
    for iid in instruments:
        slug = iid.split(".")[0] if "." in iid else iid
        try:
            ts = int(slug.split("-")[-1])
        except:
            ts = int(now_ms // 1000)
        ts_ms = ts * 1000
        cm = CandidateMarket(
            market_id=slug, instrument_id=iid,
            up=CandidateOutcome(instrument_id=iid), down=CandidateOutcome(instrument_id=iid),
            source_identity=None, selection_outcome="Current",
            start_ts_ms=ts_ms, expiration_ts_ms=ts_ms + 5*60*1000, seconds_to_end=300
        )
        cands.append(cm)
    if cands:
        decision = SelectionDecision(ruleset_id="updown", state=type("S",(),{"market": cands[0]})() )
        return RuntimeSelectionSnapshot(ruleset_id="updown", decision=decision, eligible_candidates=cands, published_at_ms=now_ms)
    return idle_selection_snapshot("updown", now_ms, TARGET_MARKET_NOT_FOUND_REASON)
