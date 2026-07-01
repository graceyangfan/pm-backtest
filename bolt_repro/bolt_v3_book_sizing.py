"""
Basic port of OutcomeBookState and related from bolt-v2/src/bolt_v3_book_sizing.rs

For consistency: maintain book levels from OrderBookDeltas, compute executable prices.
"""

from dataclasses import dataclass, field
from typing import Optional, Dict
from collections import OrderedDict  # or BTreeMap equiv
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.data import OrderBookDelta
from nautilus_trader.model.enums import BookAction, OrderSide
from nautilus_trader.model.objects import Price

@dataclass
class OutcomeBookState:
    instrument_id: Optional[InstrumentId] = None
    last_observed_instrument_id: Optional[InstrumentId] = None
    bid_levels: Dict[Price, float] = field(default_factory=dict)  # price -> size
    ask_levels: Dict[Price, float] = field(default_factory=dict)
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    liquidity_available: Optional[float] = None

    @classmethod
    def empty(cls) -> "OutcomeBookState":
        return cls()

    def update_from_delta(self, delta: OrderBookDelta):
        """Update internal levels from a delta (simplified; bolt-v2 has full)."""
        action = delta.action
        if action == BookAction.CLEAR or (delta.order and float(delta.order.price) == 0.0 and action == 4):
            self.bid_levels.clear()
            self.ask_levels.clear()
            self.best_bid = None
            self.best_ask = None
            return

        if not delta.order or not delta.order.price:
            return
        price = float(delta.order.price)
        size = float(delta.order.size) if delta.order.size else 0.0
        pkey = delta.order.price  # keep Price for dict key if wanted, but use float for simplicity

        # Use float keys for ease with our data
        if delta.order.side == OrderSide.BUY:
            if size > 0:
                self.bid_levels[price] = size
            else:
                self.bid_levels.pop(price, None)
            if self.bid_levels:
                self.best_bid = max(self.bid_levels.keys())
            else:
                self.best_bid = None
        else:
            if size > 0:
                self.ask_levels[price] = size
            else:
                self.ask_levels.pop(price, None)
            if self.ask_levels:
                self.best_ask = min(self.ask_levels.keys())
            else:
                self.best_ask = None

    def executable_price_for_order_side(self, side: OrderSide) -> Optional[float]:
        """Simplified executable (aggressive) price."""
        if side == OrderSide.BUY and self.best_ask:
            return self.best_ask
        if side == OrderSide.SELL and self.best_bid:
            return self.best_bid
        return None

# More can be added: liquidity calc, etc.
