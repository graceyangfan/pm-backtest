"""
Bolt-v2-style up/down taker strategy on top of Nautilus Trader's official APIs.

This version aligns the Polymarket semantics with a YES/NO instrument pair:
- entry YES  -> BUY YES
- entry NO   -> BUY NO
- exit YES   -> BUY NO (hedge-exit)
- exit NO    -> BUY YES (hedge-exit)

The strategy still uses Nautilus' own backtest engine, order submission, fills,
books, and expiry settlement. We only maintain the strategy-side decision state.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
from typing import Optional

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import InstrumentClose
from nautilus_trader.model.book import OrderBook
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.events import OrderCanceled, OrderExpired, OrderFilled, OrderRejected
from nautilus_trader.model.events import PositionChanged, PositionClosed, PositionOpened
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy

from bolt_repro.bolt_v3_binary_outcome_edge import (
    BinaryOutcomeEdgeInputs,
    evaluate_binary_outcome_edge,
)
from bolt_repro.bolt_v3_book_sizing import OutcomeBookState
from bolt_repro.bolt_v3_executable_cost import (
    ExecutableCostBreakdown,
    compute_executable_cost_from_book,
)
from bolt_repro.bolt_v3_sizing import RobustSizingInputs, choose_robust_size
from bolt_repro.bolt_v3_taker_updown_signal import (
    SideSelectionInputs,
    ThetaScalerInputs,
    UncertaintyBandInputs,
    WorstCaseEvInputs,
    choose_entry_side,
    compute_theta_scaler,
    compute_worst_case_ev_bps,
    fair_probability_up,
    uncertainty_band_probability,
)


@dataclass
class ReferenceSample:
    second: int
    mid: float


@dataclass
class PendingEntryState:
    client_order_id: object
    instrument_id: InstrumentId
    outcome_side: str


@dataclass
class OpenPositionState:
    outcome_side: str
    position_id: object | None
    instrument_id: InstrumentId
    entry_price: float
    entry_fee_bps: float
    quantity: float


@dataclass
class PendingExitState:
    client_order_id: object
    position: OpenPositionState
    forced_flat_reasons: list[str]
    filled_quantity: float = 0.0
    fill_received: bool = False
    close_received: bool = False


@dataclass
class ExitEvaluation:
    position_outcome_side: str | None
    forced_flat_reasons: list[str]
    hold_ev_bps: float | None
    exit_ev_bps: float | None
    exit_decision: str | None
    blocked_reason: str | None


@dataclass
class ExitSubmissionDecision:
    evaluation: ExitEvaluation
    instrument_id: InstrumentId | None
    order_side: OrderSide | None
    quantity: float | None
    price: float | None
    blocked_reason: str | None


@dataclass
class EntryEvaluation:
    fair_probability_up: float
    realized_vol: float
    seconds_to_expiry: int
    uncertainty_band_probability: float
    lead_gap_probability: float
    jitter_penalty_probability: float
    up_edge_bps: float
    down_edge_bps: float
    up_worst_ev_bps: float | None
    down_worst_ev_bps: float | None
    min_worst_case_ev_bps: float
    selected_side: str | None


class BoltUpdownTakerConfig(StrategyConfig, frozen=True):
    yes_instrument_id: str
    no_instrument_id: str
    reference_instrument_id: str
    start_price: float
    interval_start_ns: int
    interval_end_ns: int
    edge_threshold_bps: float = 5.0
    exit_hysteresis_bps: float = 5.0
    theta_decay_factor: float = 1.5
    pricing_kurtosis: float = 0.0
    order_notional_target: float = 100.0
    maximum_position_notional: float = 100.0
    risk_lambda: float = 0.5
    sizing_ev_reference_bps: float = 500.0
    book_impact_cap_bps: int = 15
    vwap_depth_limit_bps: int = 200
    slippage_buffer_bps: int = 0
    cadence_seconds: int = 300
    rv_window_seconds: int = 300
    rv_min_returns: int = 30
    reentry_cooldown_secs: int = 15
    forced_flat_stale_reference_ms: int = 1_500
    forced_flat_thin_book_min_liquidity: float = 100.0
    lead_jitter_max_ms: int = 250
    entry_time_in_force: TimeInForce = TimeInForce.FOK
    exit_time_in_force: TimeInForce = TimeInForce.IOC


class BoltUpdownTaker(Strategy):
    EXIT_DECISION_HOLD = "hold"
    EXIT_DECISION_EXIT = "exit"
    EXIT_DECISION_FAIL_CLOSED = "exit_fail_closed"
    EXIT_BLOCK_NO_OPEN_POSITION = "no_open_position"
    EXIT_BLOCK_EXIT_ALREADY_PENDING = "exit_already_pending"
    EXIT_BLOCK_ENTRY_ORDER_STILL_WORKING = "entry_order_still_working"
    EXIT_BLOCK_EXIT_HOLD = "exit_hold"
    EXIT_BLOCK_EXIT_DECISION_UNAVAILABLE = "exit_decision_unavailable"
    EXIT_BLOCK_EXIT_PRICE_MISSING = "exit_price_missing"
    EXIT_BLOCK_EXIT_QUANTITY_NOT_POSITIVE = "exit_quantity_not_positive"

    def __init__(self, config: BoltUpdownTakerConfig):
        super().__init__(config)
        self.yes_id = InstrumentId.from_str(config.yes_instrument_id)
        self.no_id = InstrumentId.from_str(config.no_instrument_id)
        self.reference_id = InstrumentId.from_str(config.reference_instrument_id)
        self._reference_samples: deque[ReferenceSample] = deque()
        self._last_reference_sample_second: Optional[int] = None
        self._last_reference_ts_ns: Optional[int] = None
        self._last_reference_interval_ns: Optional[int] = None
        self._last_reference_jitter_ns: Optional[int] = None
        self._last_signal_second: Optional[int] = None
        self._last_exit_ns = 0
        self._pending_entry: Optional[PendingEntryState] = None
        self._open_position: Optional[OpenPositionState] = None
        self._pending_exit: Optional[PendingExitState] = None

    def on_start(self):
        for instrument_id in (self.yes_id, self.no_id, self.reference_id):
            self.request_instrument(instrument_id)
            self.subscribe_order_book_deltas(instrument_id)
        for instrument_id in (self.yes_id, self.no_id):
            self.subscribe_instrument_close(instrument_id)
        self.log.info(
            f"Started BoltUpdownTaker yes={self.yes_id} no={self.no_id} "
            f"ref={self.reference_id} start_price={self.config.start_price}",
        )

    def on_stop(self):
        self.log.info("Stopped BoltUpdownTaker")

    def _exposure_occupancy(self) -> str:
        if self._pending_exit is not None:
            return "exit_pending"
        if self._open_position is not None:
            return "managed_position"
        if self._pending_entry is not None:
            return "pending_entry"
        return "flat"

    def on_order_rejected(self, event: OrderRejected):
        self._handle_terminal_order(event.client_order_id, "rejected")

    def on_order_canceled(self, event: OrderCanceled):
        self._handle_terminal_order(event.client_order_id, "canceled")

    def on_order_expired(self, event: OrderExpired):
        self._handle_terminal_order(event.client_order_id, "expired")

    def on_order_filled(self, event: OrderFilled):
        instrument_id = event.instrument_id
        fill_qty = float(event.last_qty)
        fill_px = float(event.last_px)
        commission = float(event.commission)
        fee_bps = 0.0 if fill_qty <= 0.0 or fill_px <= 0.0 else (commission / (fill_qty * fill_px)) * 10_000.0

        if self._pending_entry is not None and event.client_order_id == self._pending_entry.client_order_id:
            self.log.info(
                f"entry_fill side={self._pending_entry.outcome_side} qty={fill_qty:.6f} "
                f"px={fill_px:.4f} fee_bps={fee_bps:.2f}",
            )
            return

        pending_exit = self._pending_exit
        if pending_exit is None or event.client_order_id != pending_exit.client_order_id:
            return

        pending_exit.filled_quantity += fill_qty
        pending_exit.fill_received = True
        self._last_exit_ns = int(event.ts_event)
        remaining_qty = max(0.0, pending_exit.position.quantity - pending_exit.filled_quantity)
        if remaining_qty <= 1e-9:
            self._open_position = None
            self._pending_exit = None
        else:
            self._open_position = OpenPositionState(
                outcome_side=pending_exit.position.outcome_side,
                instrument_id=pending_exit.position.instrument_id,
                entry_price=pending_exit.position.entry_price,
                entry_fee_bps=pending_exit.position.entry_fee_bps,
                quantity=remaining_qty,
            )
            self._pending_exit = None
        self.log.info(
            f"exit_fill side={pending_exit.position.outcome_side} qty={fill_qty:.6f} "
            f"px={fill_px:.4f} remaining={remaining_qty:.6f}",
        )

    def on_position_opened(self, event: PositionOpened):
        if self._pending_entry is None:
            return
        if event.instrument_id != self._pending_entry.instrument_id:
            return
        if event.signed_qty <= 0:
            return
        self._open_position = OpenPositionState(
            outcome_side=self._pending_entry.outcome_side,
            position_id=event.position_id,
            instrument_id=event.instrument_id,
            entry_price=float(event.avg_px_open),
            entry_fee_bps=self._infer_entry_fee_bps(event.avg_px_open),
            quantity=float(event.quantity),
        )
        self._pending_entry = None
        self.log.info(
            f"position_opened side={self._open_position.outcome_side} "
            f"position_id={event.position_id} qty={float(event.quantity):.6f} avg_px_open={float(event.avg_px_open):.4f}",
        )

    def on_position_changed(self, event: PositionChanged):
        if self._open_position is None:
            return
        if event.instrument_id != self._open_position.instrument_id:
            return
        if event.signed_qty <= 0:
            return
        self._open_position = OpenPositionState(
            outcome_side=self._open_position.outcome_side,
            position_id=event.position_id,
            instrument_id=event.instrument_id,
            entry_price=float(event.avg_px_open),
            entry_fee_bps=self._open_position.entry_fee_bps,
            quantity=float(event.quantity),
        )

    def on_position_closed(self, event: PositionClosed):
        if self._open_position is not None and event.instrument_id == self._open_position.instrument_id:
            self.log.info(f"position_closed position_id={event.position_id} instrument={event.instrument_id}")
            self._open_position = None
        if self._pending_exit is not None and event.instrument_id == self._pending_exit.position.instrument_id:
            self._pending_exit.close_received = True
            self._pending_exit = None

    def on_instrument_close(self, update: InstrumentClose):
        instrument_id = update.instrument_id
        if self._pending_entry is not None and self._pending_entry.instrument_id == instrument_id:
            self.log.warning(f"pending_entry_cleared_on_close instrument={instrument_id}")
            self._pending_entry = None
        if self._pending_exit is not None and self._pending_exit.position.instrument_id == instrument_id:
            self._pending_exit.close_received = True
            self._pending_exit = None
            self._open_position = None
            self.log.info(f"exit_terminal_on_close instrument={instrument_id}")
        elif self._open_position is not None and self._open_position.instrument_id == instrument_id:
            self._open_position = None
            self.log.info(f"position_cleared_on_close instrument={instrument_id}")

    def on_order_book_deltas(self, deltas):
        instrument_id = deltas.instrument_id
        ts_ns = int(deltas.ts_event)

        if instrument_id == self.reference_id:
            self._update_reference_samples(ts_ns)
            return

        if instrument_id not in {self.yes_id, self.no_id}:
            return
        if ts_ns < self.config.interval_start_ns or ts_ns > self.config.interval_end_ns:
            return

        signal_second = ts_ns // 1_000_000_000
        if self._last_signal_second == signal_second:
            return
        self._last_signal_second = signal_second

        yes_book = self.cache.order_book(self.yes_id)
        no_book = self.cache.order_book(self.no_id)
        ref_book = self.cache.order_book(self.reference_id)
        if yes_book is None or no_book is None or ref_book is None:
            return

        spot_mid = self._book_midpoint(ref_book)
        if spot_mid is None:
            return

        yes_state = self._snapshot_book(self.yes_id, yes_book)
        no_state = self._snapshot_book(self.no_id, no_book)
        if yes_state.best_ask is None or no_state.best_ask is None:
            return

        evaluation = self._build_entry_evaluation(ts_ns, spot_mid, yes_state, no_state)
        if evaluation is None:
            return

        selected_side = evaluation.selected_side
        self.log.info(
            f"signal ts={ts_ns} spot={spot_mid:.2f} start={self.config.start_price:.2f} "
            f"rv={evaluation.realized_vol:.6f} fair_up={evaluation.fair_probability_up:.6f} "
            f"ub={evaluation.uncertainty_band_probability:.6f} "
            f"lead_gap={evaluation.lead_gap_probability:.6f} "
            f"jitter={evaluation.jitter_penalty_probability:.6f} "
            f"up_edge={evaluation.up_edge_bps:.2f} down_edge={evaluation.down_edge_bps:.2f} "
            f"up_worst={evaluation.up_worst_ev_bps} down_worst={evaluation.down_worst_ev_bps} "
            f"threshold={evaluation.min_worst_case_ev_bps:.2f} selected={selected_side}",
        )

        if self._open_position is None and self._pending_exit is None:
            if selected_side is None or ts_ns < self._last_exit_ns + self.config.reentry_cooldown_secs * 1_000_000_000:
                return
            if self._pending_entry is not None:
                self.log.info(f"entry_blocked reason=pending_entry occupancy={self._exposure_occupancy()} ts={ts_ns}")
                return
            if self._forced_flat_reasons(ts_ns, yes_state, no_state):
                return
            self._submit_entry(selected_side, ts_ns, yes_state, no_state, evaluation)
            return

        if self._open_position is not None:
            decision = self._exit_submission_decision(
                ts_ns,
                evaluation.fair_probability_up,
                evaluation.uncertainty_band_probability,
                yes_state,
                no_state,
            )
            self._maybe_submit_exit(ts_ns, decision)

    def _update_reference_samples(self, ts_ns: int) -> None:
        book = self.cache.order_book(self.reference_id)
        mid = self._book_midpoint(book)
        if mid is None:
            return
        if self._last_reference_ts_ns is not None:
            current_interval_ns = ts_ns - self._last_reference_ts_ns
            if self._last_reference_interval_ns is not None:
                self._last_reference_jitter_ns = abs(current_interval_ns - self._last_reference_interval_ns)
            else:
                self._last_reference_jitter_ns = 0
            self._last_reference_interval_ns = current_interval_ns
        self._last_reference_ts_ns = ts_ns
        second = ts_ns // 1_000_000_000
        if self._last_reference_sample_second == second:
            return
        self._last_reference_sample_second = second
        self._reference_samples.append(ReferenceSample(second=second, mid=mid))
        cutoff = second - self.config.rv_window_seconds - 2
        while self._reference_samples and self._reference_samples[0].second < cutoff:
            self._reference_samples.popleft()

    def _current_realized_vol(self) -> Optional[float]:
        mids = [sample.mid for sample in self._reference_samples]
        if len(mids) < self.config.rv_min_returns + 1:
            return None
        log_returns = [
            math.log(mids[i] / mids[i - 1])
            for i in range(1, len(mids))
            if mids[i] > 0.0 and mids[i - 1] > 0.0
        ]
        if len(log_returns) < self.config.rv_min_returns:
            return None
        variance = sum(value * value for value in log_returns) / (len(log_returns) - 1)
        return math.sqrt(variance) * math.sqrt(365.25 * 24.0 * 3600.0)

    def _build_entry_evaluation(
        self,
        ts_ns: int,
        spot_mid: float,
        yes_state: OutcomeBookState,
        no_state: OutcomeBookState,
    ) -> Optional[EntryEvaluation]:
        realized_vol = self._current_realized_vol()
        if realized_vol is None:
            return None

        seconds_to_expiry = max(0, int((self.config.interval_end_ns - ts_ns) // 1_000_000_000))
        if seconds_to_expiry <= 0:
            return None

        fair_probability = fair_probability_up(
            spot_mid,
            self.config.start_price,
            seconds_to_expiry,
            realized_vol=realized_vol,
            kurtosis=self.config.pricing_kurtosis,
        )
        if fair_probability is None:
            return None

        yes_cost = self._build_cost_breakdown(yes_state, self.config.order_notional_target)
        no_cost = self._build_cost_breakdown(no_state, self.config.order_notional_target)
        if yes_cost is None or no_cost is None:
            return None

        yes_fee_bps = self._fee_bps_from_cost(yes_cost)
        no_fee_bps = self._fee_bps_from_cost(no_cost)
        lead_gap_probability = self._lead_gap_probability()
        jitter_penalty_probability = self._jitter_penalty_probability()
        if jitter_penalty_probability is None:
            return None

        uncertainty_band = uncertainty_band_probability(
            UncertaintyBandInputs(
                lead_gap_probability=lead_gap_probability,
                jitter_penalty_probability=jitter_penalty_probability,
                time_uncertainty_probability=max(
                    0.0,
                    min(1.0, 1.0 - seconds_to_expiry / self.config.cadence_seconds),
                ),
                fee_uncertainty_probability=max(yes_fee_bps, no_fee_bps) / 10_000.0,
            ),
        )
        if uncertainty_band is None:
            return None

        up_edge = evaluate_binary_outcome_edge(
            BinaryOutcomeEdgeInputs(
                side="Up",
                fair_probability_up=fair_probability,
                adjusted_probability_up=max(0.0, min(1.0, fair_probability - uncertainty_band)),
                order_side="Buy",
                cost_breakdown=yes_cost,
                minimum_edge_bps=0.0,
            ),
        )
        down_edge = evaluate_binary_outcome_edge(
            BinaryOutcomeEdgeInputs(
                side="Down",
                fair_probability_up=fair_probability,
                adjusted_probability_up=max(0.0, min(1.0, fair_probability + uncertainty_band)),
                order_side="Buy",
                cost_breakdown=no_cost,
                minimum_edge_bps=0.0,
            ),
        )

        theta = compute_theta_scaler(
            ThetaScalerInputs(
                seconds_to_market_end=seconds_to_expiry,
                cadence_seconds=self.config.cadence_seconds,
                theta_decay_factor=self.config.theta_decay_factor,
            ),
        )
        if theta is None:
            return None
        min_edge_bps = self.config.edge_threshold_bps * theta

        up_worst_ev_bps = compute_worst_case_ev_bps(
            "Up",
            WorstCaseEvInputs(
                fair_probability=fair_probability,
                uncertainty_band_probability=uncertainty_band,
                executable_entry_cost=yes_cost.vwap_price,
                fee_bps=yes_fee_bps,
            ),
        )
        down_worst_ev_bps = compute_worst_case_ev_bps(
            "Down",
            WorstCaseEvInputs(
                fair_probability=fair_probability,
                uncertainty_band_probability=uncertainty_band,
                executable_entry_cost=no_cost.vwap_price,
                fee_bps=no_fee_bps,
            ),
        )
        selected_side = choose_entry_side(
            SideSelectionInputs(
                up_worst_ev_bps=up_worst_ev_bps,
                down_worst_ev_bps=down_worst_ev_bps,
                min_worst_case_ev_bps=min_edge_bps,
            ),
        )

        return EntryEvaluation(
            fair_probability_up=fair_probability,
            realized_vol=realized_vol,
            seconds_to_expiry=seconds_to_expiry,
            uncertainty_band_probability=uncertainty_band,
            lead_gap_probability=lead_gap_probability,
            jitter_penalty_probability=jitter_penalty_probability,
            up_edge_bps=up_edge.edge_bps,
            down_edge_bps=down_edge.edge_bps,
            up_worst_ev_bps=up_worst_ev_bps,
            down_worst_ev_bps=down_worst_ev_bps,
            min_worst_case_ev_bps=min_edge_bps,
            selected_side=selected_side,
        )

    def _submit_entry(
        self,
        selected_side: str,
        ts_ns: int,
        yes_state: OutcomeBookState,
        no_state: OutcomeBookState,
        evaluation: EntryEvaluation,
    ) -> None:
        target_book = yes_state if selected_side == "Up" else no_state
        target_edge_bps = evaluation.up_edge_bps if selected_side == "Up" else evaluation.down_edge_bps
        book_impact_cap_notional = self._book_impact_cap_notional(target_book)
        sized_notional = choose_robust_size(
            RobustSizingInputs(
                expected_ev_per_notional=(target_edge_bps or 0.0) / 10_000.0,
                ev_reference_per_notional=self.config.sizing_ev_reference_bps / 10_000.0,
                risk_lambda=self.config.risk_lambda,
                order_notional_target=self.config.order_notional_target,
                maximum_position_notional=self.config.maximum_position_notional,
                impact_cap_notional=book_impact_cap_notional,
            ),
        )
        if sized_notional <= 0.0 or target_book.best_ask is None:
            return

        instrument_id = self.yes_id if selected_side == "Up" else self.no_id
        instrument = self.cache.instrument(instrument_id)
        if instrument is None:
            return

        quantity = instrument.make_qty(sized_notional / target_book.best_ask)
        order_price = instrument.make_price(target_book.best_ask)
        order = self.order_factory.limit(
            instrument_id=instrument_id,
            order_side=OrderSide.BUY,
            quantity=quantity,
            price=order_price,
            time_in_force=self.config.entry_time_in_force,
        )
        self._pending_entry = PendingEntryState(
            client_order_id=order.client_order_id,
            instrument_id=instrument_id,
            outcome_side=selected_side,
        )
        self.submit_order(order)
        self.log.info(
            f"submit_entry side={selected_side} instrument={instrument_id} "
            f"sized_notional={sized_notional:.4f} qty={float(quantity):.6f} "
            f"px={float(order_price):.4f} tif={self.config.entry_time_in_force} ts={ts_ns}",
        )

    def _exit_submission_decision(
        self,
        ts_ns: int,
        fair_p_up: float,
        uncertainty_band_probability_value: float,
        yes_state: OutcomeBookState,
        no_state: OutcomeBookState,
    ) -> ExitSubmissionDecision:
        open_position = self._open_position
        if open_position is None or self._pending_exit is not None:
            return ExitSubmissionDecision(
                evaluation=ExitEvaluation(
                    position_outcome_side=None if open_position is None else open_position.outcome_side,
                    forced_flat_reasons=[],
                    hold_ev_bps=None,
                    exit_ev_bps=None,
                    exit_decision=None,
                    blocked_reason=self.EXIT_BLOCK_NO_OPEN_POSITION if open_position is None else self.EXIT_BLOCK_EXIT_ALREADY_PENDING,
                ),
                instrument_id=None,
                order_side=None,
                quantity=None,
                price=None,
                blocked_reason=self.EXIT_BLOCK_NO_OPEN_POSITION if open_position is None else self.EXIT_BLOCK_EXIT_ALREADY_PENDING,
            )

        forced_flat_reasons = self._forced_flat_reasons(ts_ns, yes_state, no_state)
        evaluation = ExitEvaluation(
            position_outcome_side=open_position.outcome_side,
            forced_flat_reasons=forced_flat_reasons,
            hold_ev_bps=None,
            exit_ev_bps=None,
            exit_decision=None,
            blocked_reason=None,
        )
        if forced_flat_reasons:
            held_state = self._held_book(open_position, yes_state, no_state)
            return ExitSubmissionDecision(
                evaluation=ExitEvaluation(
                    position_outcome_side=open_position.outcome_side,
                    forced_flat_reasons=forced_flat_reasons,
                    hold_ev_bps=None,
                    exit_ev_bps=None,
                    exit_decision=self.EXIT_DECISION_EXIT,
                    blocked_reason=None,
                ),
                instrument_id=open_position.instrument_id,
                order_side=OrderSide.SELL,
                quantity=open_position.quantity,
                price=held_state.best_bid,
                blocked_reason=None if held_state.best_bid else self.EXIT_BLOCK_EXIT_PRICE_MISSING,
            )

        if self._pending_entry is not None:
            evaluation.blocked_reason = self.EXIT_BLOCK_ENTRY_ORDER_STILL_WORKING
            return ExitSubmissionDecision(evaluation, None, None, None, None, evaluation.blocked_reason)

        hold_ev_bps = compute_worst_case_ev_bps(
            open_position.outcome_side,
            WorstCaseEvInputs(
                fair_probability=fair_p_up,
                uncertainty_band_probability=uncertainty_band_probability_value,
                executable_entry_cost=open_position.entry_price,
                fee_bps=open_position.entry_fee_bps,
            ),
        )
        if hold_ev_bps is None:
            evaluation.exit_decision = self.EXIT_DECISION_FAIL_CLOSED
            held_state = self._held_book(open_position, yes_state, no_state)
            return ExitSubmissionDecision(
                evaluation=ExitEvaluation(
                    position_outcome_side=open_position.outcome_side,
                    forced_flat_reasons=[],
                    hold_ev_bps=None,
                    exit_ev_bps=None,
                    exit_decision=self.EXIT_DECISION_FAIL_CLOSED,
                    blocked_reason=None,
                ),
                instrument_id=open_position.instrument_id,
                order_side=OrderSide.SELL,
                quantity=open_position.quantity,
                price=held_state.best_bid,
                blocked_reason=None if held_state.best_bid else self.EXIT_BLOCK_EXIT_PRICE_MISSING,
            )

        held_state = self._held_book(open_position, yes_state, no_state)
        if held_state.best_bid is None or held_state.best_bid <= 0.0:
            evaluation.hold_ev_bps = hold_ev_bps
            evaluation.exit_decision = self.EXIT_DECISION_FAIL_CLOSED
            evaluation.blocked_reason = self.EXIT_BLOCK_EXIT_PRICE_MISSING
            return ExitSubmissionDecision(evaluation, None, None, None, None, evaluation.blocked_reason)

        exit_price = held_state.best_bid
        exit_fee_bps = self._pm_fee_bps(open_position.quantity, exit_price)
        total_entry_cost = open_position.entry_price * (1.0 + open_position.entry_fee_bps / 10_000.0)
        net_exit_value = exit_price * (1.0 - exit_fee_bps / 10_000.0)
        exit_ev_bps = ((net_exit_value - total_entry_cost) / total_entry_cost) * 10_000.0
        exit_decision = self._evaluate_exit_decision(hold_ev_bps, exit_ev_bps)
        evaluation = ExitEvaluation(
            position_outcome_side=open_position.outcome_side,
            forced_flat_reasons=[],
            hold_ev_bps=hold_ev_bps,
            exit_ev_bps=exit_ev_bps,
            exit_decision=exit_decision,
            blocked_reason=None if exit_decision != self.EXIT_DECISION_HOLD else self.EXIT_BLOCK_EXIT_HOLD,
        )
        if exit_decision == self.EXIT_DECISION_HOLD:
            return ExitSubmissionDecision(evaluation, None, None, None, None, self.EXIT_BLOCK_EXIT_HOLD)
        return ExitSubmissionDecision(
            evaluation=evaluation,
            instrument_id=open_position.instrument_id,
            order_side=OrderSide.SELL,
            quantity=open_position.quantity,
            price=exit_price,
            blocked_reason=None,
        )

    def _snapshot_book(self, instrument_id: InstrumentId, order_book: OrderBook) -> OutcomeBookState:
        state = OutcomeBookState(instrument_id=instrument_id)
        for level in order_book.bids():
            state.bid_levels[float(level.price)] = float(level.size())
        for level in order_book.asks():
            state.ask_levels[float(level.price)] = float(level.size())
        best_bid = order_book.best_bid_price()
        best_ask = order_book.best_ask_price()
        state.best_bid = None if best_bid is None else float(best_bid)
        state.best_ask = None if best_ask is None else float(best_ask)
        return state

    def _build_cost_breakdown(
        self,
        book_state: OutcomeBookState,
        notional: float,
    ) -> Optional[ExecutableCostBreakdown]:
        raw = compute_executable_cost_from_book(
            book_state,
            "Buy",
            notional=notional,
            fee_bps=0.0,
            slippage_bps=self.config.slippage_buffer_bps,
            depth_limit_bps=self.config.vwap_depth_limit_bps,
        )
        if not raw.cost_available or raw.vwap_price is None or raw.vwap_quantity is None:
            return None
        fee_bps = self._pm_fee_bps(raw.vwap_quantity, raw.vwap_price)
        raw.fee_cost_cents = raw.gross_cost_cents * fee_bps / 10_000.0
        raw.total_adjusted_cost_cents = raw.gross_cost_cents + raw.fee_cost_cents + raw.slippage_buffer_cents
        return raw

    def _book_impact_cap_notional(self, book_state: OutcomeBookState) -> float:
        levels = sorted(book_state.ask_levels.items())
        best_touch = book_state.best_ask
        if best_touch is None:
            return 0.0
        depth_limit = self.config.book_impact_cap_bps / 10_000.0
        allowed = best_touch * (1.0 + depth_limit)
        total = 0.0
        for price, size in levels:
            if price > allowed:
                break
            total += price * size
        return total

    def _fee_bps_from_cost(self, cost: ExecutableCostBreakdown) -> float:
        if not cost.cost_available or not cost.gross_cost_cents or not cost.fee_cost_cents:
            return 0.0
        return max(0.0, (cost.fee_cost_cents / cost.gross_cost_cents) * 10_000.0)

    def _lead_gap_probability(self) -> float:
        # With only one fast/reference venue in the current replay, there is no
        # independent cross-venue anchor to measure a lead/reference gap against.
        return 0.0

    def _jitter_penalty_probability(self) -> Optional[float]:
        if self._last_reference_jitter_ns is None:
            return None
        if self.config.lead_jitter_max_ms <= 0:
            return 0.0
        return max(
            0.0,
            min(
                1.0,
                (self._last_reference_jitter_ns / 1_000_000.0) / self.config.lead_jitter_max_ms,
            ),
        )

    def _held_book(
        self,
        open_position: OpenPositionState,
        yes_state: OutcomeBookState,
        no_state: OutcomeBookState,
    ) -> OutcomeBookState:
        return yes_state if open_position.instrument_id == self.yes_id else no_state

    def _submit_exit(
        self,
        ts_ns: int,
        open_position: OpenPositionState,
        forced_flat_reasons: list[str],
        best_bid: float | None,
        hold_ev_bps: float | None = None,
        exit_ev_bps: float | None = None,
    ) -> None:
        instrument = self.cache.instrument(open_position.instrument_id)
        if instrument is None or best_bid is None or best_bid <= 0.0:
            return
        order = self.order_factory.market(
            instrument_id=open_position.instrument_id,
            order_side=OrderSide.SELL,
            quantity=instrument.make_qty(open_position.quantity),
            time_in_force=self.config.exit_time_in_force,
        )
        self._pending_exit = PendingExitState(
            client_order_id=order.client_order_id,
            position=open_position,
            forced_flat_reasons=list(forced_flat_reasons),
        )
        self.submit_order(order)
        self.log.info(
            f"submit_exit side={open_position.outcome_side} instrument={open_position.instrument_id} "
            f"px_ref={best_bid:.4f} hold_ev={hold_ev_bps} exit_ev={exit_ev_bps} "
            f"forced_flat={forced_flat_reasons} ts={ts_ns}",
        )

    def _maybe_submit_exit(self, ts_ns: int, decision: ExitSubmissionDecision) -> None:
        if decision.blocked_reason is not None:
            self.log.info(
                f"exit_blocked reason={decision.blocked_reason} side={decision.evaluation.position_outcome_side} "
                f"forced_flat={decision.evaluation.forced_flat_reasons} hold_ev={decision.evaluation.hold_ev_bps} "
                f"exit_ev={decision.evaluation.exit_ev_bps} ts={ts_ns}",
            )
            return
        open_position = self._open_position
        if open_position is None or decision.instrument_id is None or decision.price is None:
            return
        self._submit_exit(
            ts_ns,
            open_position,
            decision.evaluation.forced_flat_reasons,
            best_bid=decision.price,
            hold_ev_bps=decision.evaluation.hold_ev_bps,
            exit_ev_bps=decision.evaluation.exit_ev_bps,
        )

    def _evaluate_exit_decision(self, hold_ev_bps: float | None, exit_ev_bps: float | None) -> str:
        if hold_ev_bps is None or exit_ev_bps is None:
            return self.EXIT_DECISION_FAIL_CLOSED
        if not math.isfinite(hold_ev_bps) or not math.isfinite(exit_ev_bps) or not math.isfinite(self.config.exit_hysteresis_bps):
            return self.EXIT_DECISION_FAIL_CLOSED
        if exit_ev_bps >= hold_ev_bps - self.config.exit_hysteresis_bps:
            return self.EXIT_DECISION_EXIT
        return self.EXIT_DECISION_HOLD

    def _infer_entry_fee_bps(self, avg_px_open: float) -> float:
        if self._pending_entry is None or avg_px_open <= 0.0:
            return 0.0
        return self._pm_fee_bps(1.0, avg_px_open)

    def _handle_terminal_order(self, client_order_id: object, reason: str) -> None:
        if self._pending_entry is not None and client_order_id == self._pending_entry.client_order_id:
            self.log.info(f"pending_entry_terminal reason={reason} client_order_id={client_order_id}")
            self._pending_entry = None
            return
        if self._pending_exit is not None and client_order_id == self._pending_exit.client_order_id:
            self.log.info(f"pending_exit_terminal reason={reason} client_order_id={client_order_id}")
            self._open_position = self._pending_exit.position
            self._pending_exit = None

    def _forced_flat_reasons(
        self,
        ts_ns: int,
        yes_state: OutcomeBookState,
        no_state: OutcomeBookState,
    ) -> list[str]:
        reasons: list[str] = []
        if self._last_reference_ts_ns is None:
            reasons.append("reference_missing")
        else:
            age_ms = max(0.0, (ts_ns - self._last_reference_ts_ns) / 1_000_000.0)
            if age_ms > self.config.forced_flat_stale_reference_ms:
                reasons.append("stale_reference")
        liquidity = min(
            self._book_impact_cap_notional(yes_state),
            self._book_impact_cap_notional(no_state),
        )
        if liquidity < self.config.forced_flat_thin_book_min_liquidity:
            reasons.append("thin_book")
        return reasons

    def _pm_fee_bps(self, quantity: float, price: float) -> float:
        gross_cost = quantity * price
        if gross_cost <= 0.0:
            return 0.0
        fee_amount = 0.07 * quantity * price * (1.0 - price)
        return fee_amount / gross_cost * 10_000.0

    def _book_midpoint(self, book: Optional[OrderBook]) -> Optional[float]:
        if book is None:
            return None
        midpoint = book.midpoint()
        return None if midpoint is None else float(midpoint)
