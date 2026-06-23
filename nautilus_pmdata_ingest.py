#!/usr/bin/env python3
"""
PMData.dev -> Nautilus Trader 独立转换脚本（Polymarket only）

完全在当前 pm_backtest 目录实现。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from decimal import Decimal
from typing import Any

import pandas as pd

from nautilus_trader.model.instruments import BinaryOption
from nautilus_trader.model.currencies import Currency
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.persistence.catalog import ParquetDataCatalog

from nautilus_trader.core import nautilus_pyo3 as na_pyo3


API_BASE = "https://api.pmdata.dev"
DEFAULT_API_KEY = os.getenv("PMDATA_API_KEY", "sk-UW15uNF3oQGdbmTLbnNlGcQHq51UNAZt")
DEFAULT_USER_AGENT = "pm-backtest/1.0"

# All data and catalog must live under this root.
PM_BACKTEST_ROOT = "/Users/yfclark/pm_backtest"
TARGET_CATALOG = "/Users/yfclark/pm_backtest/catalog"
DATA_DIR = "/Users/yfclark/pm_backtest/data"


def download_pmdata(slug: str, data_type: str = "poly_l2", api_key: str = DEFAULT_API_KEY, force: bool = False) -> pd.DataFrame:
    """Download from pmdata.dev and save to ./data/ in current directory."""
    os.makedirs(DATA_DIR, exist_ok=True)
    local_path = os.path.join(DATA_DIR, f"{slug}.parquet")

    if not force and os.path.exists(local_path):
        print(f"Local file exists: {local_path} (skipping download)")
        return pd.read_parquet(local_path)

    url = f"{API_BASE}/download/{data_type}/{slug}.parquet"
    print(f"Downloading {url} -> {local_path}")
    df = pd.read_parquet(url, storage_options={"api_key": api_key, "User-Agent": DEFAULT_USER_AGENT})
    df.to_parquet(local_path)
    print(f"  Saved locally. rows={len(df)}, events={dict(df['event_type'].value_counts())}")
    return df


async def _fetch_gamma_market(slug: str) -> dict:
    """从 Gamma API (Polymarket adapter style) 获取市场信息，用于获取真实精度、费用等。"""
    client = na_pyo3.HttpClient()
    url = f"https://gamma-api.polymarket.com/markets?slug={slug}"
    resp = await client.get(url, timeout_secs=30)
    if resp.status != 200:
        raise RuntimeError(f"Failed to fetch {slug}: status {resp.status}")
    markets = json.loads(resp.body)
    if not markets:
        raise ValueError(f"No market found for slug {slug}")
    return markets[0]

def _parse_activation_ns_from_slug(slug: str) -> int:
    """从 slug 解析 activation 时间 (unix seconds -> ns)。
    e.g. btc-updown-5m-1778803200 -> 1778803200 * 1e9
    """
    try:
        ts = int(slug.split('-')[-1])
        return ts * 1_000_000_000
    except Exception:
        return 0


_DURATION_MAP = {
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
}


def _parse_duration_seconds_from_slug(slug: str) -> int:
    """从 slug 解析市场持续时间（秒）。
    slug 格式: coin-updown-5m-UNIX 或 coin-updown-15m-UNIX 等。
    """
    try:
        # 找倒数第二个 token，如 5m / 15m
        parts = slug.split('-')
        if len(parts) >= 2:
            dur_token = parts[-2].lower()
            if dur_token in _DURATION_MAP:
                return _DURATION_MAP[dur_token]
            # 兼容带数字的如 1h
            for k, v in _DURATION_MAP.items():
                if dur_token.endswith(k) or dur_token == k:
                    return v
    except Exception:
        pass
    # 默认 5m
    return 300

def get_pm_instrument(slug: str) -> BinaryOption:
    """
    Get Polymarket instrument, preferring direct fetch from adapter (Gamma + parse_polymarket_instrument).

    If the market cannot be fetched from the live API (common for historical/archived updown slugs),
    falls back to a clean minimal construction informed by:
    - The Nautilus Polymarket adapter's parse_polymarket_instrument (pUSD, size 1e-6, fees, etc.)
    - The reference project /Users/yfclark/quant_study/prediction-market-backtesting (uses parse when possible)
    - Slug encoding for activation/expiration times.

    This is the design used in the reference project: try real data first, have a reasonable
    construction for cases where live data is not available.
    """
    activation_ns = _parse_activation_ns_from_slug(slug)
    duration_s = _parse_duration_seconds_from_slug(slug)
    expiration_ns = activation_ns + duration_s * 1_000_000_000 if activation_ns > 0 else 0

    try:
        market = asyncio.run(_fetch_gamma_market(slug))
        from nautilus_trader.adapters.polymarket.common.gamma_markets import normalize_gamma_market_to_clob_format
        from nautilus_trader.adapters.polymarket.common.parsing import parse_polymarket_instrument

        market = normalize_gamma_market_to_clob_format(market)
        clob_tokens = market.get("clobTokenIds", "[]")
        if isinstance(clob_tokens, str):
            clob_tokens = json.loads(clob_tokens)
        token_id = clob_tokens[0] if clob_tokens else "0"
        outcome = "Yes" if "up" in slug.lower() else "Outcome"

        inst = parse_polymarket_instrument(market, token_id, outcome)
        # IMPORTANT: Use slug-based ID (not the condition-token ID from parse_polymarket_instrument)
        # for compatibility with pmdata.dev historical parquet keys.
        # Real adapter (symbol.py + parsing.py:214) uses get_polymarket_instrument_id(condition, token).
        # These updown-5m/15m markets are short-lived and we match the data source keys here.
        py_iid = na_pyo3.InstrumentId.from_str(f"{slug}.POLYMARKET")
        pyo3_inst = na_pyo3.BinaryOption(
            instrument_id=py_iid,
            raw_symbol=na_pyo3.Symbol(slug),
            asset_class=na_pyo3.AssetClass.ALTERNATIVE,
            currency=na_pyo3.Currency.from_str("pUSD"),
            price_precision=inst.price_precision,
            size_precision=inst.size_precision,
            price_increment=na_pyo3.Price.from_str(str(inst.price_increment)),
            size_increment=na_pyo3.Quantity.from_str(str(inst.size_increment)),
            activation_ns=activation_ns,
            # Prefer the real end_date_iso from Gamma/adapter when available and sane.
            # parse_polymarket_instrument (parsing.py:225-231) derives expiration from end_date_iso
            # (or +10y fallback) and sets activation_ns=0 (#TBD in adapter).
            # For historical/archived updown slugs that often 404 on Gamma, we fall back to
            # slug-derived activation + duration (5m=300s, 15m=900s etc.).
            expiration_ns=(inst.expiration_ns if inst.expiration_ns > activation_ns else expiration_ns),
            # Use activation as ts_event/ts_init for the instrument definition.
            # This gives a stable "market start" timestamp for these historical short markets.
            # parse_polymarket_instrument uses time.time_ns() or provided ts_init.
            ts_event=activation_ns or 0,
            ts_init=activation_ns or 0,
            outcome=inst.outcome,
            description=inst.description,
            maker_fee=inst.maker_fee,
            taker_fee=inst.taker_fee,
            info=inst.info or {"source": "gamma+adapter", "slug": slug},
        )
        result = BinaryOption.from_pyo3(pyo3_inst)
        print(f"✓ Got PM instrument from adapter for {slug}")
        return result
    except Exception as e:
        print(f"Could not fetch {slug} from adapter ({e}), using informed minimal construction.")
        return make_minimal_instrument(slug)


def make_minimal_instrument(slug: str) -> BinaryOption:
    """
    Clean fallback construction for PM instrument when live fetch from Gamma/adapter is not possible
    (e.g. archived historical updown markets).

    Informed by:
    - Nautilus Polymarket adapter (pUSD, size=1e-6, fees from market or standard 0/0.07)
      See parsing.py:217 (price from minimum_tick_size), :224 (size=0.000001), :243 (pUSD),
      and extract_fee_rates.
    - Reference project (parse when possible, reasonable defaults otherwise)
    - Slug encoding: btc-updown-5m-TS  => activation=TS, duration=300s
                      ...-15m-TS      => activation=TS, duration=900s

    expiration = activation + duration (from slug token)
    Note: parse_polymarket_instrument sets activation_ns=0. We override using slug for these
    short-duration prediction markets.
    """
    activation_ns = _parse_activation_ns_from_slug(slug)
    duration_s = _parse_duration_seconds_from_slug(slug)
    expiration_ns = activation_ns + duration_s * 1_000_000_000 if activation_ns > 0 else 0

    py_iid = na_pyo3.InstrumentId.from_str(f"{slug}.POLYMARKET")
    pyo3_inst = na_pyo3.BinaryOption(
        instrument_id=py_iid,
        raw_symbol=na_pyo3.Symbol(slug),
        asset_class=na_pyo3.AssetClass.ALTERNATIVE,
        currency=na_pyo3.Currency.from_str("pUSD"),
        price_precision=3,
        size_precision=6,
        price_increment=na_pyo3.Price.from_str("0.001"),
        size_increment=na_pyo3.Quantity.from_str("0.000001"),
        activation_ns=activation_ns,
        expiration_ns=expiration_ns,
        ts_event=activation_ns,
        ts_init=activation_ns,
        outcome="Yes" if "up" in slug.lower() else "Outcome",
        description=f"Polymarket {slug} (pmdata)",
        maker_fee=Decimal("0"),
        taker_fee=Decimal("0.07"),
        info={"source": "minimal+slug", "slug": slug, "category": "crypto", "feeRate": 0.07},
    )
    return BinaryOption.from_pyo3(pyo3_inst)


def _to_ns(ts: Any) -> int:
    """Convert pmdata "timestamp" to nanoseconds.

    What it actually is for pmdata.dev:
    The "timestamp" column is **seconds** (unix timestamp, float with fractional part is fine).
    Verified by inspecting multiple pmdata parquet files + usage in other polymarket repos.
    No heuristics. Direct conversion.
    """
    if pd.isna(ts):
        return 0
    return int(pd.to_datetime(float(ts), unit="s", utc=True).value)


def pmdata_parquet_to_nautilus_pyo3(
    df: pd.DataFrame,
    instrument: BinaryOption,
) -> tuple[list[Any], list[Any]]:
    """直接使用原生 pyO3 对象构造。"""
    df = df.sort_values("timestamp").reset_index(drop=True)
    py_iid = na_pyo3.InstrumentId.from_str(str(instrument.id))

    pyo3_deltas_batches: list[Any] = []
    pyo3_trades: list[Any] = []

    for _, row in df.iterrows():
        ev = str(row.get("event_type", ""))
        ts_event = _to_ns(row.get("timestamp"))
        ts_init = _to_ns(row.get("local_timestamp") or row.get("timestamp"))

        if ev == "book":
            bp = row.get("bid_prices")
            bs = row.get("bid_sizes")
            ap = row.get("ask_prices")
            asz = row.get("ask_sizes")
            bids_p = list(bp) if bp is not None else []
            bids_s = list(bs) if bs is not None else []
            asks_p = list(ap) if ap is not None else []
            asks_s = list(asz) if asz is not None else []

            deltas: list[Any] = []
            clear = na_pyo3.OrderBookDelta(
                instrument_id=py_iid,
                action=na_pyo3.BookAction.CLEAR,
                order=None,
                flags=na_pyo3.RecordFlag.F_SNAPSHOT,
                sequence=0,
                ts_event=ts_event,
                ts_init=ts_init,
            )
            deltas.append(clear)

            for i, (p, s) in enumerate(zip(bids_p, bids_s)):
                if p is None or s is None or float(s) <= 0:
                    continue
                flags = na_pyo3.RecordFlag.F_SNAPSHOT
                if (i == len(bids_p) - 1) and len(asks_p) == 0:
                    flags |= na_pyo3.RecordFlag.F_LAST
                price = na_pyo3.Price.from_str(str(float(p)))
                size = na_pyo3.Quantity.from_str(str(float(s)))
                bo = na_pyo3.BookOrder(side=na_pyo3.OrderSide.BUY, price=price, size=size, order_id=0)
                deltas.append(na_pyo3.OrderBookDelta(instrument_id=py_iid, action=na_pyo3.BookAction.ADD, order=bo, flags=flags, sequence=0, ts_event=ts_event, ts_init=ts_init))

            for i, (p, s) in enumerate(zip(asks_p, asks_s)):
                if p is None or s is None or float(s) <= 0:
                    continue
                flags = na_pyo3.RecordFlag.F_SNAPSHOT
                if i == len(asks_p) - 1:
                    flags |= na_pyo3.RecordFlag.F_LAST
                price = na_pyo3.Price.from_str(str(float(p)))
                size = na_pyo3.Quantity.from_str(str(float(s)))
                bo = na_pyo3.BookOrder(side=na_pyo3.OrderSide.SELL, price=price, size=size, order_id=0)
                deltas.append(na_pyo3.OrderBookDelta(instrument_id=py_iid, action=na_pyo3.BookAction.ADD, order=bo, flags=flags, sequence=0, ts_event=ts_event, ts_init=ts_init))

            if len(deltas) > 1:
                batch = na_pyo3.OrderBookDeltas(instrument_id=py_iid, deltas=deltas)
                pyo3_deltas_batches.append(batch)

        elif ev == "price_change":
            p = row.get("pc_price")
            s = row.get("pc_size")
            side_str = str(row.get("pc_side") or "").upper()
            if p is None or pd.isna(p): continue
            side = na_pyo3.OrderSide.BUY if side_str == "BUY" else na_pyo3.OrderSide.SELL
            qty_f = 0.0 if (pd.isna(s) or float(s) <= 0) else float(s)
            action = na_pyo3.BookAction.DELETE if qty_f <= 0 else na_pyo3.BookAction.UPDATE
            price = na_pyo3.Price.from_str(str(float(p)))
            size = na_pyo3.Quantity.from_str(str(qty_f))
            bo = na_pyo3.BookOrder(side=side, price=price, size=size, order_id=0)
            delta = na_pyo3.OrderBookDelta(instrument_id=py_iid, action=action, order=bo, flags=na_pyo3.RecordFlag.F_LAST, sequence=0, ts_event=ts_event, ts_init=ts_init)
            batch = na_pyo3.OrderBookDeltas(instrument_id=py_iid, deltas=[delta])
            pyo3_deltas_batches.append(batch)

        elif ev == "last_trade_price":
            price_f = row.get("trade_price")
            size_f = row.get("trade_size")
            side_str = str(row.get("trade_side") or "").upper()
            if price_f is None or pd.isna(price_f): continue
            aggressor = na_pyo3.AggressorSide.BUYER if side_str == "BUY" else na_pyo3.AggressorSide.SELLER
            price = na_pyo3.Price.from_str(str(float(price_f)))
            size = na_pyo3.Quantity.from_str(str(float(size_f) if size_f is not None and not pd.isna(size_f) else 0.0))
            trade = na_pyo3.TradeTick(instrument_id=py_iid, price=price, size=size, aggressor_side=aggressor, trade_id=na_pyo3.TradeId(str(row.get("hash") or f"pm-{ts_event}")), ts_event=ts_event, ts_init=ts_init)
            pyo3_trades.append(trade)

    return pyo3_deltas_batches, pyo3_trades


def _write_instrument_and_data(catalog_path: str, instrument: BinaryOption, deltas: list, trades: list = None):
    """简洁写入：先 instrument，再 data。pyo3 对象。"""
    os.makedirs(catalog_path, exist_ok=True)
    cat = ParquetDataCatalog(catalog_path)
    cat.write_data([instrument])
    if deltas:
        cat.write_data(deltas)
    if trades:
        cat.write_data(trades)
    print(f"Written to {catalog_path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--slug", required=True)
    parser.add_argument("--data-type", default="poly_l2")
    parser.add_argument("--catalog", default=TARGET_CATALOG)
    parser.add_argument("--api-key", default=DEFAULT_API_KEY)
    parser.add_argument("--download-only", action="store_true")
    parser.add_argument("--clean", action="store_true")
    parser.add_argument("--force-download", action="store_true")
    args = parser.parse_args()

    if args.clean:
        import shutil
        for p in (TARGET_CATALOG, DATA_DIR):
            if os.path.exists(p):
                shutil.rmtree(p, ignore_errors=True)
                print(f"Removed previous {p}")
        os.makedirs(DATA_DIR, exist_ok=True)
        os.makedirs(TARGET_CATALOG, exist_ok=True)

    df = download_pmdata(args.slug, args.data_type, args.api_key, force=args.force_download)

    if args.download_only:
        print(f"Download complete. Data in ./data/{args.slug}.parquet")
        return

    instrument = get_pm_instrument(args.slug)
    pyo3_deltas, pyo3_trades = pmdata_parquet_to_nautilus_pyo3(df, instrument)

    os.makedirs(DATA_DIR, exist_ok=True)

    print(f"Writing instrument + {len(pyo3_deltas)} native pyO3 OrderBookDeltas + {len(pyo3_trades)} native pyO3 trades")
    _write_instrument_and_data(TARGET_CATALOG, instrument, pyo3_deltas, pyo3_trades)

    cat = ParquetDataCatalog(TARGET_CATALOG)
    print("Catalog instruments:", [str(i.id) for i in cat.instruments()])


if __name__ == "__main__":
    main()
