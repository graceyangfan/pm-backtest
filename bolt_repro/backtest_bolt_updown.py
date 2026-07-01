"""
Official Nautilus Trader backtest entrypoint for the bolt-v2-style up/down
strategy against the local catalog.

This version uses:
- YES/NO dual-instrument Polymarket writes from `nautilus_pmdata_ingest.py`
- official `BacktestEngine`
- catalog `InstrumentClose` events for expiry settlement
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pandas as pd

sys.path.insert(0, "/Users/yfclark/nautilus_trader")
sys.path.insert(0, "/Users/yfclark/pm_backtest")

from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import BacktestEngineConfig, DataEngineConfig
from nautilus_trader.model.book import OrderBook
from nautilus_trader.model.currencies import USDT, pUSD
from nautilus_trader.model.enums import AccountType, BookType, OmsType
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Money
from nautilus_trader.persistence.catalog import ParquetDataCatalog

from bolt_repro.bolt_updown_taker_strategy import BoltUpdownTaker, BoltUpdownTakerConfig
from bolt_repro.start_price import PolymarketStartPriceProvider
from polymarket_fee_model import PolymarketFeeModel


CATALOG_PATH = "/Users/yfclark/pm_backtest/catalog"
START_PRICE_CACHE_PATH = Path("/Users/yfclark/pm_backtest/bolt_repro/start_price_cache.json")
PM_SLUG = "btc-updown-5m-1781827200"
PM_YES_INSTRUMENT_ID = f"{PM_SLUG}-YES.POLYMARKET"
PM_NO_INSTRUMENT_ID = f"{PM_SLUG}-NO.POLYMARKET"
OKX_INSTRUMENT_ID = "BTC-USDT.OKX"
PM_PREOPEN_SECONDS = 300
PM_POST_RESOLVE_SECONDS = 600
RV_WINDOW_SECONDS = 300
RV_MIN_RETURNS = 30
RV_WARMUP_EXTRA_SECONDS = 30


def load_cache() -> dict:
    if not START_PRICE_CACHE_PATH.exists():
        return {}
    return json.loads(START_PRICE_CACHE_PATH.read_text())


def save_cache(cache: dict) -> None:
    START_PRICE_CACHE_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True))


def resolve_start_price(catalog: ParquetDataCatalog, slug: str, start_sec: int) -> tuple[float, str]:
    cache = load_cache()
    cached = cache.get(slug)
    if cached and int(cached["start_sec"]) == start_sec:
        return float(cached["price"]), str(cached["source"])

    provider = PolymarketStartPriceProvider(
        request_timeout_secs=5.0,
        max_retries=1,
        retry_backoff_secs=0.0,
        cache_ttl_secs=3600,
        cache_max_items=32,
    )
    snapshot = provider.resolve_by_slug_start_sec(slug, start_sec)
    if snapshot is not None:
        price = float(snapshot.k_price)
        source = str(snapshot.source)
    else:
        okx = catalog.order_book_deltas(
            instrument_ids=[OKX_INSTRUMENT_ID],
            start=pd.Timestamp(start_sec, unit="s", tz="UTC"),
            end=pd.Timestamp(start_sec + 30, unit="s", tz="UTC"),
        )
        if not okx:
            raise RuntimeError("Could not resolve startPrice from web or OKX fallback")

        book = OrderBook(okx[0].instrument_id, BookType.L2_MBP)
        price = 0.0
        for delta in okx:
            book.apply_delta(delta)
            midpoint = book.midpoint()
            if midpoint is not None:
                price = float(midpoint)
                break
        if price <= 0.0:
            raise RuntimeError("Could not derive OKX fallback startPrice from L2 deltas")
        source = "okx_mid_fallback"

    cache[slug] = {"slug": slug, "start_sec": start_sec, "price": price, "source": source}
    save_cache(cache)
    return price, source


def main() -> None:
    catalog = ParquetDataCatalog(CATALOG_PATH)
    yes_instrument = catalog.instruments(instrument_ids=[PM_YES_INSTRUMENT_ID])[0]
    no_instrument = catalog.instruments(instrument_ids=[PM_NO_INSTRUMENT_ID])[0]
    okx_instrument = catalog.instruments(instrument_ids=[OKX_INSTRUMENT_ID])[0]

    start_sec = int(PM_SLUG.rsplit("-", 1)[1])
    end_sec = start_sec + 300
    start_price, start_price_source = resolve_start_price(catalog, PM_SLUG, start_sec)
    rv_warmup_seconds = max(RV_WINDOW_SECONDS, RV_MIN_RETURNS) + RV_WARMUP_EXTRA_SECONDS
    data_prestart_seconds = max(PM_PREOPEN_SECONDS, rv_warmup_seconds)

    pm_deltas = catalog.order_book_deltas(
        instrument_ids=[PM_YES_INSTRUMENT_ID, PM_NO_INSTRUMENT_ID],
        start=pd.Timestamp(start_sec - data_prestart_seconds, unit="s", tz="UTC"),
        end=pd.Timestamp(end_sec + PM_POST_RESOLVE_SECONDS, unit="s", tz="UTC"),
    )
    pm_closes = catalog.instrument_closes(
        instrument_ids=[PM_YES_INSTRUMENT_ID, PM_NO_INSTRUMENT_ID],
        start=pd.Timestamp(start_sec, unit="s", tz="UTC"),
        end=pd.Timestamp(end_sec + PM_POST_RESOLVE_SECONDS, unit="s", tz="UTC"),
    )
    okx_deltas = catalog.order_book_deltas(
        instrument_ids=[OKX_INSTRUMENT_ID],
        start=pd.Timestamp(start_sec - rv_warmup_seconds, unit="s", tz="UTC"),
        end=pd.Timestamp(end_sec, unit="s", tz="UTC"),
    )

    engine = BacktestEngine(
        config=BacktestEngineConfig(
            trader_id="BACKTEST-BOLT",
            data_engine=DataEngineConfig(buffer_deltas=True),
        ),
    )
    engine.add_venue(
        venue=Venue("POLYMARKET"),
        oms_type=OmsType.NETTING,
        account_type=AccountType.CASH,
        starting_balances=[Money(1_000_000, pUSD)],
        fee_model=PolymarketFeeModel(),
    )
    engine.add_venue(
        venue=Venue("OKX"),
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        starting_balances=[Money(1_000_000, USDT)],
    )
    engine.add_instrument(yes_instrument)
    engine.add_instrument(no_instrument)
    engine.add_instrument(okx_instrument)

    strategy = BoltUpdownTaker(
        BoltUpdownTakerConfig(
            yes_instrument_id=PM_YES_INSTRUMENT_ID,
            no_instrument_id=PM_NO_INSTRUMENT_ID,
            reference_instrument_id=OKX_INSTRUMENT_ID,
            start_price=start_price,
            interval_start_ns=start_sec * 1_000_000_000,
            interval_end_ns=end_sec * 1_000_000_000,
            edge_threshold_bps=5.0,
            exit_hysteresis_bps=5.0,
            theta_decay_factor=1.5,
            pricing_kurtosis=0.0,
            order_notional_target=100.0,
            maximum_position_notional=100.0,
            risk_lambda=0.5,
            sizing_ev_reference_bps=500.0,
            book_impact_cap_bps=15,
            vwap_depth_limit_bps=15,
            slippage_buffer_bps=0,
            cadence_seconds=300,
            rv_window_seconds=RV_WINDOW_SECONDS,
            rv_min_returns=RV_MIN_RETURNS,
            reentry_cooldown_secs=15,
            forced_flat_stale_reference_ms=1_500,
            forced_flat_thin_book_min_liquidity=100.0,
            lead_jitter_max_ms=250,
        ),
    )
    engine.add_strategy(strategy)

    engine.add_data(okx_deltas, sort=False)
    engine.add_data(pm_deltas, sort=False)
    engine.add_data(pm_closes, sort=False)
    engine.sort_data()

    print("=== Nautilus backtest ===")
    print(f"pm_slug={PM_SLUG}")
    print(f"pm_yes={PM_YES_INSTRUMENT_ID}")
    print(f"pm_no={PM_NO_INSTRUMENT_ID}")
    print(f"okx_instrument={OKX_INSTRUMENT_ID}")
    print(f"start_price={start_price:.6f} source={start_price_source}")
    print(f"rv_warmup_seconds={rv_warmup_seconds}")
    print(f"okx_deltas={len(okx_deltas)} pm_deltas={len(pm_deltas)} pm_closes={len(pm_closes)}")

    engine.run()

    positions = list(engine.cache.positions())
    print(f"positions_total={len(positions)}")
    for position in positions[-10:]:
        print(
            f"position instrument={position.instrument_id} side={position.side} "
            f"signed_qty={position.signed_qty} avg_px_open={position.avg_px_open} "
            f"avg_px_close={position.avg_px_close} ts_closed={position.ts_closed}",
        )

    engine.dispose()


if __name__ == "__main__":
    main()
