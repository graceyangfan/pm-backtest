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

def get_pm_instrument(slug: str) -> BinaryOption:
    """
    尝试像 OKX 一样从 adapter (Gamma API) 获取 instrument，使用真实市场数据。
    - currency = pUSD (不是 USD)
    - price/size precision & increment 来自市场 minimum_tick_size
    - activation_ns = slug 解析的实际开始时间
    - expiration_ns = activation + 300s (5m) + 900s (15min update buffer)
    - 其他从 market 或 adapter parse 获取
    不要硬编码 0 或默认值。
    """
    activation_ns = _parse_activation_ns_from_slug(slug)
    expiration_ns = activation_ns + 300 * 1_000_000_000 + 900 * 1_000_000_000 if activation_ns > 0 else 0

    try:
        market = asyncio.run(_fetch_gamma_market(slug))
        # 优先用 adapter 的 parse 获取规范的 Python BinaryOption (有 pUSD, 真实 tick, fees, exp)
        from nautilus_trader.adapters.polymarket.common.parsing import parse_polymarket_instrument
        clob_tokens = market.get("clobTokenIds", "[]")
        if isinstance(clob_tokens, str):
            clob_tokens = json.loads(clob_tokens)
        token_id = clob_tokens[0] if clob_tokens else "0"
        outcome = "Yes" if "up" in slug.lower() else "Outcome"
        inst = parse_polymarket_instrument(market, token_id, outcome)

        # 覆盖 activation/expiration 为 slug 驱动的准确值 (adapter 当前 activation_ns=0)
        # 重建 pyo3 用真实值
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
            expiration_ns=expiration_ns,
            ts_event=0,
            ts_init=0,
            outcome=inst.outcome,
            description=inst.description,
            maker_fee=inst.maker_fee,
            taker_fee=inst.taker_fee,
            info=inst.info or {"source": "gamma+slug", "slug": slug},
        )
        result = BinaryOption.from_pyo3(pyo3_inst)
        print(f"✓ Got PM instrument from adapter (pUSD + real times) for {slug}")
        return result
    except Exception as e:
        print(f"Adapter fetch failed for PM {slug} ({e}), using slug-derived fallback.")
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
            ts_event=0,
            ts_init=0,
            outcome="Yes" if "up" in slug.lower() else "Outcome",
            description=f"Polymarket {slug} (pmdata)",
            maker_fee=Decimal("0"),
            taker_fee=Decimal("0.07"),
            info={"source": "pmdata.dev+slug", "slug": slug, "category": "crypto"},
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
    parser.add_argument("--catalog", default="/Users/yfclark/pm_backtest/catalog")
    parser.add_argument("--api-key", default=DEFAULT_API_KEY)
    parser.add_argument("--download-only", action="store_true")
    parser.add_argument("--clean", action="store_true")
    parser.add_argument("--force-download", action="store_true")
    args = parser.parse_args()

    if args.clean:
        import shutil
        cat_path = args.catalog or TARGET_CATALOG
        if os.path.exists(cat_path):
            shutil.rmtree(cat_path, ignore_errors=True)
            print(f"Removed previous catalog: {cat_path}")
        if os.path.exists(DATA_DIR):
            shutil.rmtree(DATA_DIR, ignore_errors=True)
            print(f"Removed previous data dir: {DATA_DIR}")
        os.makedirs(DATA_DIR, exist_ok=True)

    df = download_pmdata(args.slug, args.data_type, args.api_key, force=args.force_download)

    if args.download_only:
        print(f"Download complete. Data in ./data/{args.slug}.parquet")
        return

    instrument = get_pm_instrument(args.slug)
    pyo3_deltas, pyo3_trades = pmdata_parquet_to_nautilus_pyo3(df, instrument)

    os.makedirs(DATA_DIR, exist_ok=True)

    print(f"Writing instrument + {len(pyo3_deltas)} native pyO3 OrderBookDeltas + {len(pyo3_trades)} native pyO3 trades")
    _write_instrument_and_data(args.catalog, instrument, pyo3_deltas, pyo3_trades)

    cat = ParquetDataCatalog(args.catalog)
    print("Catalog instruments:", [str(i.id) for i in cat.instruments()])


if __name__ == "__main__":
    main()
