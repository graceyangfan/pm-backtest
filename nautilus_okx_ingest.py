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
import json
import os
import shutil
import tarfile
from typing import Any

import requests

from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.instruments import CurrencyPair, instruments_from_pyo3
from nautilus_trader.persistence.catalog import ParquetDataCatalog

from nautilus_trader.core import nautilus_pyo3 as na_pyo3

import asyncio

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
    """从 OKX live adapter 真实获取 (推荐, 避免手动构造错误)."""
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

    return asyncio.run(_fetch())


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

    all_insts = asyncio.run(_fetch_all())
    inst_map = {str(i.id).split(".")[0]: i for i in all_insts}  # e.g. "BTC-USDT" -> inst

    for sym in symbols:
        inst = inst_map.get(sym)
        if inst is None:
            # fallback to single fetch (rare)
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
    if not path.endswith(".data"):
        raise ValueError(f"Expected .data file, got: {path}")

    pyo3_deltas = []
    py_iid = na_pyo3.InstrumentId.from_str(str(instrument.id))

    # Use enum values (matches model/enums.py:438-440 and PM side usage)
    F_S = na_pyo3.RecordFlag.F_SNAPSHOT
    F_L = na_pyo3.RecordFlag.F_LAST

    # Derive expected instId from instrument for validation (e.g. "BTC-USDT")
    expected_inst = str(instrument.id).split(".")[0]

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

            # OKX L2 orderbook historical (.data NDJSON) "ts" unit is MILLISECONDS.
            # Hard rule for this source only — no guessing.
            #
            # Verified:
            # 1. OKX docs (multiple pages): "Unix timestamp format in milliseconds, e.g. 1597026383085"
            # 2. Actual file from https://www.okx.com/historical-data (2026-06-19 BTC-USDT):
            #    ts=1781827200005 → 1781827200.005 seconds = exactly the date in the filename.
            # 3. Consistent with all OKX L2 snapshot / books data.
            #
            # Nautilus = nanoseconds.
            ts = int(row.get("ts", 0))
            ts_ns = ts * 1_000_000   # ms → ns (definitive for OKX historical L2)

            bids = row.get("bids", []) or []
            asks = row.get("asks", []) or []

            # Limit to 400 levels (matches the 400lv tarball format)
            bids_lv = bids[:400]
            asks_lv = asks[:400]

            deltas = []
            # Use order=None for CLEAR to be consistent with Polymarket adapter schema
            # (schemas/book.py:70) and other Nautilus snapshot patterns.
            clear = na_pyo3.OrderBookDelta(
                instrument_id=py_iid,
                action=na_pyo3.BookAction.CLEAR,
                order=None,
                flags=F_S,
                sequence=0,
                ts_event=ts_ns,
                ts_init=ts_ns,
            )
            deltas.append(clear)

            for i, level in enumerate(bids_lv):
                if len(level) < 2:
                    continue
                size_str = str(level[1])
                if float(size_str) <= 0:
                    continue
                bo = na_pyo3.BookOrder(
                    side=na_pyo3.OrderSide.BUY,
                    price=na_pyo3.Price.from_str(str(level[0])),
                    size=na_pyo3.Quantity.from_str(size_str),
                    order_id=0,
                )
                flags = F_S
                if i == len(bids_lv) - 1 and not asks_lv:
                    flags |= F_L
                deltas.append(
                    na_pyo3.OrderBookDelta(
                        instrument_id=py_iid,
                        action=na_pyo3.BookAction.ADD,
                        order=bo,
                        flags=flags,
                        sequence=0,
                        ts_event=ts_ns,
                        ts_init=ts_ns,
                    )
                )

            for i, level in enumerate(asks_lv):
                if len(level) < 2:
                    continue
                size_str = str(level[1])
                if float(size_str) <= 0:
                    continue
                bo = na_pyo3.BookOrder(
                    side=na_pyo3.OrderSide.SELL,
                    price=na_pyo3.Price.from_str(str(level[0])),
                    size=na_pyo3.Quantity.from_str(size_str),
                    order_id=0,
                )
                flags = F_S
                if i == len(asks_lv) - 1:
                    flags |= F_L
                deltas.append(
                    na_pyo3.OrderBookDelta(
                        instrument_id=py_iid,
                        action=na_pyo3.BookAction.ADD,
                        order=bo,
                        flags=flags,
                        sequence=0,
                        ts_event=ts_ns,
                        ts_init=ts_ns,
                    )
                )

            if len(deltas) > 1:
                # Emit batch only when we have CLEAR + at least one level.
                # Pattern mirrors deribit/data.py:731 and polymarket book snapshots.
                pyo3_deltas.append(na_pyo3.OrderBookDeltas(instrument_id=py_iid, deltas=deltas))

    return pyo3_deltas


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
        deltas = load_okx_spot_l2_to_pyo3(args.data_file, inst)

        _write_instrument_and_data(TARGET_CATALOG, inst, deltas)

        cat = ParquetDataCatalog(TARGET_CATALOG)
        read = list(cat.query_order_book_deltas(instrument_ids=[inst.id]))
        print(f"Read back {len(read)} OrderBookDeltas batches")


if __name__ == "__main__":
    main()
