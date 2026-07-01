#!/usr/bin/env python3
"""
PMData.dev -> Nautilus Trader catalog writer for Polymarket up/down markets.

Design notes:
- We write two BinaryOption instruments per market: YES and NO.
- PMData historical parquet is treated as the YES-side book; the NO-side book is
  derived by complementing prices around 1.0.
- `market_resolved` rows are converted into `InstrumentClose` events so Nautilus
  can settle open YES/NO positions at expiry inside the official backtest engine.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from decimal import Decimal
from typing import Any

import pandas as pd

from nautilus_trader.core import nautilus_pyo3 as na_pyo3
from nautilus_trader.model.data import BookOrder
from nautilus_trader.model.data import InstrumentClose
from nautilus_trader.model.data import OrderBookDelta
from nautilus_trader.model.data import OrderBookDeltas
from nautilus_trader.model.data import TradeTick
from nautilus_trader.model.enums import AggressorSide
from nautilus_trader.model.enums import BookAction
from nautilus_trader.model.enums import InstrumentCloseType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.enums import RecordFlag
from nautilus_trader.model.identifiers import TradeId
from nautilus_trader.model.instruments import BinaryOption
from nautilus_trader.persistence.catalog import ParquetDataCatalog


API_BASE = "https://api.pmdata.dev"
DEFAULT_API_KEY = os.getenv("PMDATA_API_KEY", "sk-UW15uNF3oQGdbmTLbnNlGcQHq51UNAZt")
DEFAULT_USER_AGENT = "pm-backtest/1.0"

PM_BACKTEST_ROOT = "/Users/yfclark/pm_backtest"
TARGET_CATALOG = f"{PM_BACKTEST_ROOT}/catalog"
DATA_DIR = f"{PM_BACKTEST_ROOT}/data"


def download_pmdata(
    slug: str,
    data_type: str = "poly_l2",
    api_key: str = DEFAULT_API_KEY,
    force: bool = False,
) -> pd.DataFrame:
    os.makedirs(DATA_DIR, exist_ok=True)
    local_path = os.path.join(DATA_DIR, f"{slug}.parquet")

    if not force and os.path.exists(local_path):
        print(f"Local file exists: {local_path} (skipping download)")
        return pd.read_parquet(local_path)

    url = f"{API_BASE}/download/{data_type}/{slug}.parquet"
    print(f"Downloading {url} -> {local_path}")
    df = pd.read_parquet(
        url,
        storage_options={"api_key": api_key, "User-Agent": DEFAULT_USER_AGENT},
    )
    df.to_parquet(local_path)
    print(f"  Saved locally. rows={len(df)}, events={dict(df['event_type'].value_counts())}")
    return df


async def _fetch_gamma_market(slug: str) -> dict:
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
    try:
        return int(slug.split("-")[-1]) * 1_000_000_000
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
    try:
        parts = slug.split("-")
        if len(parts) >= 2:
            token = parts[-2].lower()
            if token in _DURATION_MAP:
                return _DURATION_MAP[token]
    except Exception:
        pass
    return 300


def _parse_token_outcomes(market: dict[str, Any]) -> tuple[str | None, str | None, str | None, str | None]:
    token_ids = market.get("clobTokenIds", [])
    outcomes = market.get("outcomes", [])
    if isinstance(token_ids, str):
        token_ids = json.loads(token_ids)
    if isinstance(outcomes, str):
        outcomes = json.loads(outcomes)
    if not isinstance(token_ids, list) or len(token_ids) < 2:
        return None, None, None, None

    yes_token = None
    no_token = None
    yes_outcome = "Yes"
    no_outcome = "No"
    for token_id, outcome in zip(token_ids, outcomes, strict=False):
        token = str(token_id).strip()
        outcome_str = str(outcome).strip()
        normalized = outcome_str.lower()
        if normalized in {"yes", "up", "higher", "above"} and yes_token is None:
            yes_token = token
            yes_outcome = outcome_str or "Yes"
        elif normalized in {"no", "down", "lower", "below"} and no_token is None:
            no_token = token
            no_outcome = outcome_str or "No"

    if yes_token is None:
        yes_token = str(token_ids[0]).strip()
    if no_token is None:
        no_token = str(token_ids[1]).strip()
    return yes_token, no_token, yes_outcome, no_outcome


def _surrogate_instrument_id(slug: str, side: str) -> str:
    return f"{slug}-{side.upper()}.POLYMARKET"


def _base_info(slug: str, side: str, complement_id: str) -> dict[str, Any]:
    return {
        "source": "pmdata_dual_binary",
        "slug": slug,
        "pmdata_slug": slug,
        "pair_role": side.lower(),
        "complement_instrument_id": complement_id,
        "category": "crypto",
        "feeRate": 0.07,
    }


def _make_minimal_binary_option(
    instrument_id: str,
    raw_symbol: str,
    outcome: str,
    description: str,
    activation_ns: int,
    expiration_ns: int,
    info: dict[str, Any],
) -> BinaryOption:
    pyo3_inst = na_pyo3.BinaryOption(
        instrument_id=na_pyo3.InstrumentId.from_str(instrument_id),
        raw_symbol=na_pyo3.Symbol(raw_symbol),
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
        outcome=outcome,
        description=description,
        maker_fee=Decimal("0"),
        taker_fee=Decimal("0.07"),
        info=info,
    )
    return BinaryOption.from_pyo3(pyo3_inst)


def get_pm_instruments(slug: str) -> tuple[BinaryOption, BinaryOption]:
    activation_ns = _parse_activation_ns_from_slug(slug)
    duration_s = _parse_duration_seconds_from_slug(slug)
    expiration_ns = activation_ns + duration_s * 1_000_000_000 if activation_ns > 0 else 0

    try:
        market = asyncio.run(_fetch_gamma_market(slug))
        from nautilus_trader.adapters.polymarket.common.gamma_markets import (
            normalize_gamma_market_to_clob_format,
        )
        from nautilus_trader.adapters.polymarket.common.parsing import (
            parse_polymarket_instrument,
        )

        market = normalize_gamma_market_to_clob_format(market)
        yes_token, no_token, yes_outcome, no_outcome = _parse_token_outcomes(market)
        if not yes_token or not no_token:
            raise ValueError("missing token pair")

        yes_inst = parse_polymarket_instrument(market, yes_token, yes_outcome)
        no_inst = parse_polymarket_instrument(market, no_token, no_outcome)

        yes_iid = yes_inst.id.value
        no_iid = no_inst.id.value
        yes_info = dict(yes_inst.info or {})
        no_info = dict(no_inst.info or {})
        yes_info.update(_base_info(slug, "yes", no_iid))
        no_info.update(_base_info(slug, "no", yes_iid))

        yes = _make_minimal_binary_option(
            instrument_id=yes_iid,
            raw_symbol=str(yes_inst.raw_symbol),
            outcome=yes_inst.outcome,
            description=yes_inst.description,
            activation_ns=activation_ns,
            expiration_ns=max(yes_inst.expiration_ns, expiration_ns),
            info=yes_info,
        )
        no = _make_minimal_binary_option(
            instrument_id=no_iid,
            raw_symbol=str(no_inst.raw_symbol),
            outcome=no_inst.outcome,
            description=no_inst.description,
            activation_ns=activation_ns,
            expiration_ns=max(no_inst.expiration_ns, expiration_ns),
            info=no_info,
        )
        print(f"✓ Got PM YES/NO instruments from adapter for {slug}")
        return yes, no
    except Exception as e:
        print(f"Could not fetch {slug} from adapter ({e}), using deterministic slug-based pair.")

    yes_id = _surrogate_instrument_id(slug, "yes")
    no_id = _surrogate_instrument_id(slug, "no")
    yes = _make_minimal_binary_option(
        instrument_id=yes_id,
        raw_symbol=f"{slug}-YES",
        outcome="Yes",
        description=f"Polymarket {slug} YES (pmdata)",
        activation_ns=activation_ns,
        expiration_ns=expiration_ns,
        info=_base_info(slug, "yes", no_id),
    )
    no = _make_minimal_binary_option(
        instrument_id=no_id,
        raw_symbol=f"{slug}-NO",
        outcome="No",
        description=f"Polymarket {slug} NO (pmdata)",
        activation_ns=activation_ns,
        expiration_ns=expiration_ns,
        info=_base_info(slug, "no", yes_id),
    )
    return yes, no


def _to_ns(ts: Any) -> int:
    if pd.isna(ts):
        return 0
    return int(pd.to_datetime(ts, utc=True).value)


def _build_trade_id(row: pd.Series, ts_event: int, side_tag: str) -> str:
    market_slug = str(row.get("market_slug") or "")
    local_ts_ns = _to_ns(row.get("local_timestamp"))
    side = str(row.get("trade_side") or "").upper()
    price = row.get("trade_price")
    size = row.get("trade_size")
    payload = f"{market_slug}|{side_tag}|{ts_event}|{local_ts_ns}|{side}|{price}|{size}"
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


def _complement_price(price: float) -> float:
    return max(0.0, min(1.0, 1.0 - price))


def _level_values(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, float) and pd.isna(value):
        return []
    return list(value)


def _make_single_delta(
    instrument: BinaryOption,
    action: BookAction,
    side: OrderSide,
    price: float,
    size: float,
    flags: int,
    ts_event: int,
    ts_init: int,
) -> OrderBookDelta:
    return OrderBookDelta(
        instrument_id=instrument.id,
        action=action,
        order=BookOrder(
            side=side,
            price=instrument.make_price(price),
            size=instrument.make_qty(size),
            order_id=0,
        ),
        flags=flags,
        sequence=0,
        ts_event=ts_event,
        ts_init=ts_init,
    )


def pmdata_parquet_to_nautilus(
    df: pd.DataFrame,
    yes_instrument: BinaryOption,
    no_instrument: BinaryOption,
) -> tuple[list[Any], list[Any], list[Any], list[Any], list[Any]]:
    sort_col = "local_timestamp" if "local_timestamp" in df.columns else "timestamp"
    df = df.sort_values(sort_col).reset_index(drop=True)

    yes_batches: list[Any] = []
    yes_trades: list[Any] = []
    no_batches: list[Any] = []
    no_trades: list[Any] = []
    closes: list[Any] = []

    f_snapshot = RecordFlag.F_SNAPSHOT
    f_last = RecordFlag.F_LAST

    for _, row in df.iterrows():
        ev = str(row.get("event_type", ""))
        ts_event = _to_ns(row.get("timestamp"))
        ts_init = _to_ns(row.get("local_timestamp") or row.get("timestamp"))

        if ev == "book":
            bids_p = _level_values(row.get("bid_prices"))
            bids_s = _level_values(row.get("bid_sizes"))
            asks_p = _level_values(row.get("ask_prices"))
            asks_s = _level_values(row.get("ask_sizes"))

            yes_deltas: list[Any] = [
                OrderBookDelta(
                    instrument_id=yes_instrument.id,
                    action=BookAction.CLEAR,
                    order=None,
                    flags=f_snapshot,
                    sequence=0,
                    ts_event=ts_event,
                    ts_init=ts_init,
                ),
            ]
            no_deltas: list[Any] = [
                OrderBookDelta(
                    instrument_id=no_instrument.id,
                    action=BookAction.CLEAR,
                    order=None,
                    flags=f_snapshot,
                    sequence=0,
                    ts_event=ts_event,
                    ts_init=ts_init,
                ),
            ]

            for p, s in zip(bids_p, bids_s):
                if p is None or s is None or float(s) <= 0.0:
                    continue
                price = round(float(p), yes_instrument.price_precision)
                size = float(s)
                yes_deltas.append(
                    _make_single_delta(
                        yes_instrument,
                        BookAction.ADD,
                        OrderSide.BUY,
                        price,
                        size,
                        f_snapshot,
                        ts_event,
                        ts_init,
                    ),
                )
                no_deltas.append(
                    _make_single_delta(
                        no_instrument,
                        BookAction.ADD,
                        OrderSide.SELL,
                        round(_complement_price(price), no_instrument.price_precision),
                        size,
                        f_snapshot,
                        ts_event,
                        ts_init,
                    ),
                )

            for p, s in zip(asks_p, asks_s):
                if p is None or s is None or float(s) <= 0.0:
                    continue
                price = round(float(p), yes_instrument.price_precision)
                size = float(s)
                yes_deltas.append(
                    _make_single_delta(
                        yes_instrument,
                        BookAction.ADD,
                        OrderSide.SELL,
                        price,
                        size,
                        f_snapshot,
                        ts_event,
                        ts_init,
                    ),
                )
                no_deltas.append(
                    _make_single_delta(
                        no_instrument,
                        BookAction.ADD,
                        OrderSide.BUY,
                        round(_complement_price(price), no_instrument.price_precision),
                        size,
                        f_snapshot,
                        ts_event,
                        ts_init,
                    ),
                )

            if len(yes_deltas) > 1:
                last = yes_deltas[-1]
                yes_deltas[-1] = OrderBookDelta(
                    instrument_id=last.instrument_id,
                    action=last.action,
                    order=last.order,
                    flags=last.flags | f_last,
                    sequence=last.sequence,
                    ts_event=last.ts_event,
                    ts_init=last.ts_init,
                )
                yes_batches.append(OrderBookDeltas(instrument_id=yes_instrument.id, deltas=yes_deltas))

            if len(no_deltas) > 1:
                last = no_deltas[-1]
                no_deltas[-1] = OrderBookDelta(
                    instrument_id=last.instrument_id,
                    action=last.action,
                    order=last.order,
                    flags=last.flags | f_last,
                    sequence=last.sequence,
                    ts_event=last.ts_event,
                    ts_init=last.ts_init,
                )
                no_batches.append(OrderBookDeltas(instrument_id=no_instrument.id, deltas=no_deltas))

        elif ev == "price_change":
            p = row.get("pc_price")
            s = row.get("pc_size")
            side_str = str(row.get("pc_side") or "").upper()
            if p is None or pd.isna(p):
                continue

            qty = 0.0 if pd.isna(s) or float(s) <= 0.0 else float(s)
            action = BookAction.DELETE if qty <= 0.0 else BookAction.UPDATE
            yes_side = OrderSide.BUY if side_str == "BUY" else OrderSide.SELL
            no_side = OrderSide.SELL if yes_side == OrderSide.BUY else OrderSide.BUY
            price = round(float(p), yes_instrument.price_precision)
            no_price = round(_complement_price(price), no_instrument.price_precision)

            yes_batches.append(
                OrderBookDeltas(
                    instrument_id=yes_instrument.id,
                    deltas=[
                        _make_single_delta(
                            yes_instrument,
                            action,
                            yes_side,
                            price,
                            qty,
                            f_last,
                            ts_event,
                            ts_init,
                        ),
                    ],
                ),
            )
            no_batches.append(
                OrderBookDeltas(
                    instrument_id=no_instrument.id,
                    deltas=[
                        _make_single_delta(
                            no_instrument,
                            action,
                            no_side,
                            no_price,
                            qty,
                            f_last,
                            ts_event,
                            ts_init,
                        ),
                    ],
                ),
            )

        elif ev == "last_trade_price":
            price_f = row.get("trade_price")
            size_f = row.get("trade_size")
            side_str = str(row.get("trade_side") or "").upper()
            if price_f is None or pd.isna(price_f):
                continue

            qty = float(size_f) if size_f is not None and not pd.isna(size_f) else 0.0
            yes_price = yes_instrument.make_price(round(float(price_f), yes_instrument.price_precision))
            no_price = no_instrument.make_price(
                round(_complement_price(float(price_f)), no_instrument.price_precision),
            )
            yes_aggressor = AggressorSide.BUYER if side_str == "BUY" else AggressorSide.SELLER
            no_aggressor = AggressorSide.SELLER if yes_aggressor == AggressorSide.BUYER else AggressorSide.BUYER

            yes_trades.append(
                TradeTick(
                    instrument_id=yes_instrument.id,
                    price=yes_price,
                    size=yes_instrument.make_qty(qty),
                    aggressor_side=yes_aggressor,
                    trade_id=TradeId(_build_trade_id(row, ts_event, "yes")),
                    ts_event=ts_event,
                    ts_init=ts_init,
                ),
            )
            no_trades.append(
                TradeTick(
                    instrument_id=no_instrument.id,
                    price=no_price,
                    size=no_instrument.make_qty(qty),
                    aggressor_side=no_aggressor,
                    trade_id=TradeId(_build_trade_id(row, ts_event, "no")),
                    ts_event=ts_event,
                    ts_init=ts_init,
                ),
            )

        elif ev == "market_resolved":
            winner = str(row.get("winning_outcome") or "").strip().lower()
            yes_close_px = 1.0 if winner in {"yes", "up", "higher", "above"} else 0.0
            no_close_px = 1.0 - yes_close_px
            closes.append(
                InstrumentClose(
                    instrument_id=yes_instrument.id,
                    close_price=yes_instrument.make_price(yes_close_px),
                    close_type=InstrumentCloseType.CONTRACT_EXPIRED,
                    ts_event=ts_event,
                    ts_init=ts_init,
                ),
            )
            closes.append(
                InstrumentClose(
                    instrument_id=no_instrument.id,
                    close_price=no_instrument.make_price(no_close_px),
                    close_type=InstrumentCloseType.CONTRACT_EXPIRED,
                    ts_event=ts_event,
                    ts_init=ts_init,
                ),
            )

    return yes_batches, yes_trades, no_batches, no_trades, closes


def _write_data(catalog_path: str, data: list[Any]) -> None:
    if not data:
        return
    ParquetDataCatalog(catalog_path).write_data(data)


def main() -> None:
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

        for path in (args.catalog, DATA_DIR):
            if os.path.exists(path):
                shutil.rmtree(path, ignore_errors=True)
                print(f"Removed previous {path}")
        os.makedirs(DATA_DIR, exist_ok=True)
        os.makedirs(args.catalog, exist_ok=True)

    df = download_pmdata(args.slug, args.data_type, args.api_key, force=args.force_download)
    if args.download_only:
        print(f"Download complete. Data in {DATA_DIR}/{args.slug}.parquet")
        return

    yes_instrument, no_instrument = get_pm_instruments(args.slug)
    yes_deltas, yes_trades, no_deltas, no_trades, closes = pmdata_parquet_to_nautilus(
        df,
        yes_instrument,
        no_instrument,
    )

    os.makedirs(args.catalog, exist_ok=True)
    _write_data(args.catalog, [yes_instrument, no_instrument])
    _write_data(args.catalog, yes_deltas)
    _write_data(args.catalog, no_deltas)
    _write_data(args.catalog, yes_trades)
    _write_data(args.catalog, no_trades)
    _write_data(args.catalog, closes)

    print(
        f"Wrote instruments + yes_deltas={len(yes_deltas)} no_deltas={len(no_deltas)} "
        f"yes_trades={len(yes_trades)} no_trades={len(no_trades)} closes={len(closes)}",
    )
    cat = ParquetDataCatalog(args.catalog)
    print("Catalog instruments:", [str(i.id) for i in cat.instruments()])


if __name__ == "__main__":
    main()
