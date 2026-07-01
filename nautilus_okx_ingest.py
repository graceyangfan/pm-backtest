#!/usr/bin/env python3
"""
OKX spot L2 historical orderbook data ingest to Nautilus catalog (pyo3 native).

只处理 OKX 现货 L2 快照数据。
- 实时获取 instrument
- 从 OKX historical-data 下载真实 tar.gz
- 解析 .data NDJSON 格式为 pyo3 OrderBookDeltas
- 写入指定 catalog

目录：/Users/yfclark/pm_backtest/catalog
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import tarfile
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import requests

from nautilus_trader.model.currencies import Currency
from nautilus_trader.model.data import BookOrder, OrderBookDelta, OrderBookDeltas
from nautilus_trader.model.enums import BookAction, OrderSide, RecordFlag
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import Symbol
from nautilus_trader.model.instruments import CurrencyPair, instruments_from_pyo3
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.persistence.catalog import ParquetDataCatalog

from nautilus_trader.core import nautilus_pyo3 as na_pyo3

PM_BACKTEST_ROOT = "/Users/yfclark/pm_backtest"
TARGET_CATALOG = "/Users/yfclark/pm_backtest/catalog"
DATA_DIR = "/Users/yfclark/pm_backtest/data"


def _write_instrument_and_data(catalog_path: str, instrument: CurrencyPair, data_batches: list):
    """简洁写入：先 instrument，再 data。使用 pyo3 对象直接写入。"""
    os.makedirs(catalog_path, exist_ok=True)
    cat = ParquetDataCatalog(catalog_path)
    cat.write_data([instrument])
    if data_batches:
        cat.write_data(data_batches)
    print(f"Written to {catalog_path}")
    print("Instruments:", [str(i.id) for i in cat.instruments()])


def get_okx_spot_instrument(symbol: str = "BTC-USDT") -> CurrencyPair:
    """Get OKX spot instrument, with offline fallback for local backtests."""
    async def _fetch() -> CurrencyPair:
        print(f"Fetching instrument {symbol} from OKX live adapter (public HTTP)...")
        client = na_pyo3.OKXHttpClient(
            api_key="", api_secret="", api_passphrase="",
            environment=na_pyo3.OKXEnvironment.LIVE,
        )
        pyo3_instruments, _ = await client.request_instruments(
            na_pyo3.OKXInstrumentType.SPOT, None
        )
        # Follow real OKX adapter: cache instruments after request (data.py:274, 556, 780)
        for py_inst in pyo3_instruments:
            client.cache_instrument(py_inst)
        all_instruments = instruments_from_pyo3(pyo3_instruments)
        target_id = InstrumentId.from_str(f"{symbol}.OKX")
        for inst in all_instruments:
            if inst.id == target_id:
                print(f"  ✓ Fetched live OKX instrument: {inst.id}")
                return inst
        raise RuntimeError(f"Could not find {symbol}")

    try:
        return asyncio.run(_fetch())
    except Exception as e:
        print(f"Live OKX instrument fetch failed ({e}), using deterministic offline fallback.")
        return _make_okx_spot_instrument_fallback(symbol)


def _make_okx_spot_instrument_fallback(symbol: str) -> CurrencyPair:
    base_code, quote_code = symbol.split("-", 1)
    price_increment = Price.from_str("0.00000001")
    size_increment = Quantity.from_str("0.00000001")
    ts_now = 0
    return CurrencyPair(
        instrument_id=InstrumentId.from_str(f"{symbol}.OKX"),
        raw_symbol=Symbol(symbol),
        base_currency=Currency.from_str(base_code),
        quote_currency=Currency.from_str(quote_code),
        price_precision=price_increment.precision,
        size_precision=size_increment.precision,
        price_increment=price_increment,
        size_increment=size_increment,
        ts_event=ts_now,
        ts_init=ts_now,
        maker_fee=Decimal("0"),
        taker_fee=Decimal("0"),
        info={"source": "offline_fallback", "symbol": symbol},
    )


def write_okx_spot_instruments(symbols: list[str] | None = None, catalog_path: str = TARGET_CATALOG):
    """写入 OKX spot instruments（总是从 live adapter 获取）。
    
    Optimized: fetch the full SPOT list once (as the pyo3 client does internally),
    then pick requested symbols. Avoids repeated full requests per symbol.
    """
    if symbols is None:
        symbols = ["BTC-USDT", "ETH-USDT", "XRP-USDT", "SOL-USDT", "DOGE-USDT"]
    os.makedirs(catalog_path, exist_ok=True)
    cat = ParquetDataCatalog(catalog_path)

    # One fetch for all requested symbols (matches how real OKX provider works)
    async def _fetch_all():
        client = na_pyo3.OKXHttpClient(
            api_key="", api_secret="", api_passphrase="",
            environment=na_pyo3.OKXEnvironment.LIVE,
        )
        pyo3_instruments, _ = await client.request_instruments(
            na_pyo3.OKXInstrumentType.SPOT, None
        )
        for py_inst in pyo3_instruments:
            client.cache_instrument(py_inst)
        return instruments_from_pyo3(pyo3_instruments)

    try:
        all_insts = asyncio.run(_fetch_all())
    except Exception as e:
        print(f"Bulk OKX instrument fetch failed ({e}), using offline fallbacks.")
        all_insts = []
    inst_map = {str(i.id).split(".")[0]: i for i in all_insts}  # e.g. "BTC-USDT" -> inst

    for sym in symbols:
        inst = inst_map.get(sym)
        if inst is None:
            inst = get_okx_spot_instrument(sym)
        cat.write_data([inst])
        print(f"✓ Written instrument: {inst.id}")


def download_okx_spot_l2_orderbook(date: str = "2026-06-19", symbol: str = "BTC-USDT", output_dir: str = "data"):
    """下载 OKX 现货 L2 orderbook 快照 tar.gz 并解压，返回 .data 文件路径。

    严格使用已知格式：
    https://static.okx.com/cdn/okx/match/orderbook/L2/400lv/daily/{YYYYMMDD}/{SYMBOL}-L2orderbook-400lv-{YYYY-MM-DD}.tar.gz
    """
    output_dir = DATA_DIR
    os.makedirs(output_dir, exist_ok=True)
    ymd = date.replace("-", "")
    ymd_dashed = date

    url = f"https://static.okx.com/cdn/okx/match/orderbook/L2/400lv/daily/{ymd}/{symbol}-L2orderbook-400lv-{ymd_dashed}.tar.gz"
    local_tar = os.path.join(output_dir, f"{symbol}-L2orderbook-400lv-{ymd_dashed}.tar.gz")

    if not os.path.exists(local_tar):
        print(f"Downloading {url}")
        r = requests.get(url, stream=True, timeout=180)
        if r.status_code != 200:
            raise RuntimeError(f"Download failed: HTTP {r.status_code}")
        with open(local_tar, "wb") as f:
            for chunk in r.iter_content(8192):
                if chunk:
                    f.write(chunk)
        print(f"Downloaded to {local_tar}")
    else:
        print(f"Using existing {local_tar}")

    extract_dir = os.path.join(output_dir, f"{symbol}_{date}")
    if os.path.exists(extract_dir):
        shutil.rmtree(extract_dir)
    os.makedirs(extract_dir, exist_ok=True)

    print(f"Extracting {local_tar} ...")
    with tarfile.open(local_tar, "r:gz") as tar:
        tar.extractall(extract_dir)

    # 严格只找 .data 文件
    for root, dirs, files in os.walk(extract_dir):
        for f in files:
            if f.endswith(".data"):
                data_path = os.path.join(root, f)
                print(f"Found data file: {data_path}")
                return data_path

    raise RuntimeError(f"No .data file found after extracting {local_tar}")


def load_okx_spot_l2_to_pyo3(path: str, instrument: CurrencyPair) -> list:
    """解析 OKX 现货 L2 orderbook .data 文件（NDJSON）为 na_pyo3.OrderBookDeltas。

    格式：每行一个 snapshot JSON
    {"instId": "...", "ts": "...", "bids": [[price, size, ...], ...], "asks": [[...]] }
    """
    pyo3_deltas = []
    for chunk in iter_okx_spot_l2_batches(path, instrument, chunk_size=20_000):
        pyo3_deltas.extend(d.to_pyo3() for d in chunk)
    return pyo3_deltas


def _parse_okx_row_to_deltas(
    row: dict[str, Any],
    instrument: CurrencyPair,
) -> OrderBookDeltas | None:
    """Parse one OKX historical row into a high-level OrderBookDeltas batch."""
    # OKX historical L2 timestamps are milliseconds.
    ts = int(row.get("ts", 0))
    ts_ns = ts * 1_000_000
    action = str(row.get("action") or "").lower()

    bids_lv = (row.get("bids", []) or [])[:400]
    asks_lv = (row.get("asks", []) or [])[:400]
    deltas: list[OrderBookDelta] = []

    if action == "snapshot":
        deltas.append(
            OrderBookDelta(
                instrument_id=instrument.id,
                action=BookAction.CLEAR,
                order=None,
                flags=RecordFlag.F_SNAPSHOT,
                sequence=0,
                ts_event=ts_ns,
                ts_init=ts_ns,
            )
        )

        for level in bids_lv:
            if len(level) < 2 or float(level[1]) <= 0:
                continue
            bo = BookOrder(
                side=OrderSide.BUY,
                price=instrument.make_price(float(level[0])),
                size=instrument.make_qty(float(level[1])),
                order_id=0,
            )
            deltas.append(
                OrderBookDelta(
                    instrument_id=instrument.id,
                    action=BookAction.ADD,
                    order=bo,
                    flags=RecordFlag.F_SNAPSHOT,
                    sequence=0,
                    ts_event=ts_ns,
                    ts_init=ts_ns,
                )
            )

        for level in asks_lv:
            if len(level) < 2 or float(level[1]) <= 0:
                continue
            bo = BookOrder(
                side=OrderSide.SELL,
                price=instrument.make_price(float(level[0])),
                size=instrument.make_qty(float(level[1])),
                order_id=0,
            )
            deltas.append(
                OrderBookDelta(
                    instrument_id=instrument.id,
                    action=BookAction.ADD,
                    order=bo,
                    flags=RecordFlag.F_SNAPSHOT,
                    sequence=0,
                    ts_event=ts_ns,
                    ts_init=ts_ns,
                )
            )
    else:
        for level in bids_lv:
            if len(level) < 2:
                continue
            size = float(level[1])
            bo = BookOrder(
                side=OrderSide.BUY,
                price=instrument.make_price(float(level[0])),
                size=instrument.make_qty(0.0 if size <= 0 else size),
                order_id=0,
            )
            deltas.append(
                OrderBookDelta(
                    instrument_id=instrument.id,
                    action=BookAction.DELETE if size <= 0 else BookAction.UPDATE,
                    order=bo,
                    flags=0,
                    sequence=0,
                    ts_event=ts_ns,
                    ts_init=ts_ns,
                )
            )

        for level in asks_lv:
            if len(level) < 2:
                continue
            size = float(level[1])
            bo = BookOrder(
                side=OrderSide.SELL,
                price=instrument.make_price(float(level[0])),
                size=instrument.make_qty(0.0 if size <= 0 else size),
                order_id=0,
            )
            deltas.append(
                OrderBookDelta(
                    instrument_id=instrument.id,
                    action=BookAction.DELETE if size <= 0 else BookAction.UPDATE,
                    order=bo,
                    flags=0,
                    sequence=0,
                    ts_event=ts_ns,
                    ts_init=ts_ns,
                )
            )

    if not deltas:
        return None

    last = deltas[-1]
    deltas[-1] = OrderBookDelta(
        instrument_id=last.instrument_id,
        action=last.action,
        order=last.order,
        flags=last.flags | RecordFlag.F_LAST,
        sequence=last.sequence,
        ts_event=last.ts_event,
        ts_init=last.ts_init,
    )
    return OrderBookDeltas(instrument_id=instrument.id, deltas=deltas)


def iter_okx_spot_l2_batches(
    path: str,
    instrument: CurrencyPair,
    chunk_size: int = 20_000,
) -> Iterator[list[OrderBookDeltas]]:
    """Stream OKX historical rows into high-level Nautilus OrderBookDeltas chunks."""
    if not path.endswith(".data"):
        raise ValueError(f"Expected .data file, got: {path}")

    # Derive expected instId from instrument for validation (e.g. "BTC-USDT")
    expected_inst = str(instrument.id).split(".")[0]
    chunk: list[OrderBookDeltas] = []

    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)

            # Optional sanity: row should match requested instrument (defensive, per review feedback)
            row_inst = row.get("instId")
            if row_inst and row_inst != expected_inst:
                continue
            batch = _parse_okx_row_to_deltas(row, instrument)
            if batch is None:
                continue
            chunk.append(batch)
            if len(chunk) >= chunk_size:
                yield chunk
                chunk = []

    if chunk:
        yield chunk


def main():
    parser = argparse.ArgumentParser(description="OKX spot L2 ingest")
    parser.add_argument("--clean", action="store_true", help="清理旧的 catalog 数据")
    parser.add_argument("--action", choices=["instruments", "download-l2", "ingest-l2"], default="instruments")
    parser.add_argument("--symbol", default="BTC-USDT")
    parser.add_argument("--symbols", default="BTC-USDT,ETH-USDT,XRP-USDT,SOL-USDT,DOGE-USDT")
    parser.add_argument("--date", default="2026-06-19", help="下载日期 YYYY-MM-DD")
    parser.add_argument("--data-file", default="", help=".data 文件路径 (ingest-l2 时使用)")
    args = parser.parse_args()

    catalog = TARGET_CATALOG

    if args.clean:
        print(f"[CLEAN] Removing old catalog and data under {PM_BACKTEST_ROOT}")
        if os.path.exists(catalog):
            shutil.rmtree(catalog, ignore_errors=True)
        if os.path.exists(DATA_DIR):
            shutil.rmtree(DATA_DIR, ignore_errors=True)
        os.makedirs(catalog, exist_ok=True)
        os.makedirs(DATA_DIR, exist_ok=True)

    if args.action == "instruments":
        syms = [s.strip() for s in args.symbols.split(",")]
        write_okx_spot_instruments(syms, TARGET_CATALOG)

    elif args.action == "download-l2":
        data_path = download_okx_spot_l2_orderbook(args.date, args.symbol)
        print(f"Data file: {data_path}")

    elif args.action == "ingest-l2":
        if not args.data_file:
            raise SystemExit("--data-file is required")
        inst = get_okx_spot_instrument(args.symbol)
        os.makedirs(TARGET_CATALOG, exist_ok=True)
        cat = ParquetDataCatalog(TARGET_CATALOG)
        cat.write_data([inst])

        written = 0
        for chunk in iter_okx_spot_l2_batches(args.data_file, inst):
            cat.write_data(chunk)
            written += len(chunk)
            print(f"Wrote {written:_} OKX delta batches...")

        # Use order_book_deltas (batched=True to get the batch objects)
        print(f"Finished writing {written:_} OrderBookDeltas batches")


if __name__ == "__main__":
    main()
