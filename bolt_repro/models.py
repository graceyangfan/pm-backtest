"""
Python models replicating key structures from bolt-v2/src/strategies/binary_oracle_edge_taker/mod.rs
and related (ActiveMarketState, ExposureState, etc.) for consistency.

These mirror the Rust structs for state management in the strategy.
Use Nautilus types where possible (InstrumentId, OrderSide, etc.).
"""

from dataclasses import dataclass, field
from typing import Optional, Dict, Any
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.enums import OrderSide, PositionSide

from bolt_repro.bolt_v3_book_sizing import OutcomeBookState as _BookStateImpl

# Simplified ports of key enums/states from bolt-v2
class SelectionPhase:
    Idle = "Idle"
    Active = "Active"
    Freeze = "Freeze"

@dataclass
class OutcomeFeeState:
    up_instrument_id: Optional[InstrumentId] = None
    down_instrument_id: Optional[InstrumentId] = None
    up_ready: bool = False
    down_ready: bool = False

    def instrument_ids(self):
        ids = []
        if self.up_instrument_id: ids.append(self.up_instrument_id)
        if self.down_instrument_id: ids.append(self.down_instrument_id)
        return ids

    @classmethod
    def empty(cls):
        return cls()

    @classmethod
    def from_market(cls, market: Any):
        # stub
        return cls()

@dataclass
class OutcomePreparedBooks:
    up: _BookStateImpl = field(default_factory=_BookStateImpl.empty)
    down: _BookStateImpl = field(default_factory=_BookStateImpl.empty)

    @classmethod
    def empty(cls):
        return cls()

    @classmethod
    def from_market(cls, market: Any):
        # In full would create from candidate up/down
        b = _BookStateImpl.empty()
        return cls(up=b, down=b)

    def metadata_matches_selection(self) -> bool:
        return True

    def is_priced(self) -> bool:
        return (self.up.best_bid is not None and self.up.best_ask is not None) or \
               (self.down.best_bid is not None and self.down.best_ask is not None)

    def any_crossed(self) -> bool:
        return False

    def minimum_liquidity(self) -> float:
        return 0.0

@dataclass
class ActiveMarketState:
    phase: str = SelectionPhase.Idle
    market_id: Optional[str] = None
    source_identity: Optional[Any] = None
    instrument_id: Optional[InstrumentId] = None
    outcome_fees: OutcomeFeeState = field(default_factory=OutcomeFeeState.empty)
    price_to_beat: Optional[float] = None
    market_selection_outcome: Any = None
    interval_start_ms: Optional[int] = None
    interval_end_ms: Optional[int] = None
    selection_published_at_ms: Optional[int] = None
    seconds_to_expiry_at_selection: Optional[int] = None
    interval_open: Optional[float] = None
    last_reference_ts_ms: Optional[int] = None
    last_resolution_ts_ms: Optional[int] = None
    resolution_strike_window_mismatch_count: int = 0
    warmup_count: int = 0
    warmup_target: int = 0
    books: OutcomePreparedBooks = field(default_factory=OutcomePreparedBooks.empty)
    trade_flow: Dict = field(default_factory=dict)
    fast_venue_incoherent: bool = False
    forced_flat: bool = False

    @classmethod
    def idle(cls):
        s = cls()
        s.phase = SelectionPhase.Idle
        s.market_selection_outcome = "Current"  # default
        s.books = OutcomePreparedBooks.empty()
        return s

    @classmethod
    def from_market(cls, market: Any, warmup_target: int = 0, phase: str = None, forced_flat: bool = False):
        phase = phase or SelectionPhase.Active
        inst_id = None
        if hasattr(market, "instrument_id"):
            try:
                from nautilus_trader.model.identifiers import InstrumentId
                inst_id = InstrumentId.from_str(market.instrument_id) if isinstance(market.instrument_id, str) else market.instrument_id
            except Exception:
                inst_id = market.instrument_id
        mkt_id = getattr(market, "market_id", None) or getattr(market, "instrument_id", None)
        return cls(
            phase=phase,
            market_id=str(mkt_id) if mkt_id else None,
            instrument_id=inst_id,
            price_to_beat=getattr(market, "price_to_beat", None),
            market_selection_outcome=getattr(market, "selection_outcome", None),
            interval_start_ms=getattr(market, "start_ts_ms", None),
            interval_end_ms=getattr(market, "expiration_ts_ms", None),
            seconds_to_expiry_at_selection=getattr(market, "seconds_to_end", None),
            warmup_target=warmup_target,
            forced_flat=forced_flat,
            books=OutcomePreparedBooks.from_market(market),
        )

    @classmethod
    def from_snapshot(cls, snapshot: Any, warmup_target: int = 0):
        # snapshot has .decision.state with market or idle
        state = getattr(snapshot, "decision", None)
        if state and hasattr(state, "state"):
            state = state.state
        if hasattr(state, "market"):
            return cls.from_market(state.market, warmup_target, SelectionPhase.Active)
        # idle case
        s = cls.idle()
        s.forced_flat = True
        return s

    def same_boundary(self, other: "ActiveMarketState") -> bool:
        return (
            self.phase == other.phase
            and self.market_id == other.market_id
            and self.instrument_id == other.instrument_id
            and self.market_selection_outcome == other.market_selection_outcome
            and self.interval_start_ms == other.interval_start_ms
            and self.interval_end_ms == other.interval_end_ms
        )

    def apply_selection_timing(self, snapshot: Any):
        # mirror Rust
        dec = getattr(snapshot, "decision", None)
        if dec is None:
            return
        st = getattr(dec, "state", dec)
        if hasattr(st, "market"):
            m = st.market
            self.selection_published_at_ms = getattr(snapshot, "published_at_ms", None)
            self.market_selection_outcome = getattr(m, "selection_outcome", self.market_selection_outcome)
            self.interval_end_ms = getattr(m, "expiration_ts_ms", self.interval_end_ms)
            self.seconds_to_expiry_at_selection = getattr(m, "seconds_to_end", self.seconds_to_expiry_at_selection)
        else:
            self.selection_published_at_ms = None

    def observe_reference_quote(self, quote: Any):
        if self.phase == SelectionPhase.Idle:
            return
        # in full sets last_reference_ts_ms etc from FastSpotObservation
        if hasattr(quote, "price") or hasattr(quote, "bid"):
            self.last_reference_ts_ms = getattr(quote, "ts_ms", None) or getattr(quote, "ts_event", None)

    # observe_resolution_strike, observe_reference_snapshot etc can be added

@dataclass
class OpenPositionState:
    market_id: Optional[str] = None
    instrument_id: Optional[InstrumentId] = None
    position_id: Optional[Any] = None
    outcome_side: Optional[str] = None
    outcome_fees: OutcomeFeeState = field(default_factory=OutcomeFeeState.empty)
    historical_entry_fee_bps: Optional[float] = None
    entry_order_side: OrderSide = OrderSide.BUY
    side: PositionSide = PositionSide.LONG
    quantity: float = 0.0
    avg_px_open: float = 0.0
    interval_open: Optional[float] = None
    selection_published_at_ms: Optional[int] = None
    seconds_to_expiry_at_selection: Optional[int] = None
    book: _BookStateImpl = field(default_factory=_BookStateImpl.empty)

class ExposureState:
    Flat = "Flat"
    # variants: Open etc can be expanded

# MarketSelectionOutcome can be str or enum in practice; use simple default in Active

