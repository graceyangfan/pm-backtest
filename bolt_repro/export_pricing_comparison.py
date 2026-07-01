from __future__ import annotations

from pathlib import Path
import sys

import matplotlib.pyplot as plt
import pandas as pd

sys.path.insert(0, "/Users/yfclark/nautilus_trader")
sys.path.insert(0, "/Users/yfclark/pm_backtest")

from nautilus_trader.model.book import OrderBook
from nautilus_trader.model.enums import BookType
from nautilus_trader.persistence.catalog import ParquetDataCatalog

from bolt_repro.backtest_bolt_updown import (
    CATALOG_PATH,
    OKX_INSTRUMENT_ID,
    PM_NO_INSTRUMENT_ID,
    PM_SLUG,
    PM_YES_INSTRUMENT_ID,
    PM_POST_RESOLVE_SECONDS,
    RV_MIN_RETURNS,
    RV_WINDOW_SECONDS,
    RV_WARMUP_EXTRA_SECONDS,
    resolve_start_price,
)
from bolt_repro.bolt_v3_taker_updown_signal import fair_probability_up


OUTPUT_DIR = Path("/Users/yfclark/pm_backtest/bolt_repro/outputs")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def midpoint(book: OrderBook) -> float | None:
    value = book.midpoint()
    return None if value is None else float(value)


def best_bid(book: OrderBook) -> float | None:
    value = book.best_bid_price()
    return None if value is None else float(value)


def best_ask(book: OrderBook) -> float | None:
    value = book.best_ask_price()
    return None if value is None else float(value)


def sampled_okx_midpoints(catalog: ParquetDataCatalog, start_sec: int, end_sec: int) -> dict[int, float]:
    warmup_seconds = max(RV_WINDOW_SECONDS, RV_MIN_RETURNS) + RV_WARMUP_EXTRA_SECONDS
    deltas = catalog.order_book_deltas(
        instrument_ids=[OKX_INSTRUMENT_ID],
        start=pd.Timestamp(start_sec - warmup_seconds, unit="s", tz="UTC"),
        end=pd.Timestamp(end_sec, unit="s", tz="UTC"),
    )
    book = OrderBook(deltas[0].instrument_id, BookType.L2_MBP)
    samples: dict[int, float] = {}
    for delta in deltas:
        book.apply_delta(delta)
        mid = midpoint(book)
        if mid is None:
            continue
        second = int(delta.ts_event) // 1_000_000_000
        if second not in samples:
            samples[second] = mid
    return samples


def sampled_polymarket_books(
    catalog: ParquetDataCatalog,
    instrument_id: str,
    start_sec: int,
    end_sec: int,
) -> pd.DataFrame:
    deltas = catalog.order_book_deltas(
        instrument_ids=[instrument_id],
        start=pd.Timestamp(start_sec, unit="s", tz="UTC"),
        end=pd.Timestamp(end_sec, unit="s", tz="UTC"),
    )
    book = OrderBook(deltas[0].instrument_id, BookType.L2_MBP)
    rows: list[dict[str, float | int]] = []
    seen_seconds: set[int] = set()
    for delta in deltas:
        book.apply_delta(delta)
        second = int(delta.ts_event) // 1_000_000_000
        if second in seen_seconds:
            continue
        seen_seconds.add(second)
        bid = best_bid(book)
        ask = best_ask(book)
        mid = midpoint(book)
        rows.append(
            {
                "second": second,
                "bid": bid,
                "ask": ask,
                "mid": mid,
            },
        )
    return pd.DataFrame(rows)


def realized_vol_annualized(mid_samples: list[float]) -> float | None:
    if len(mid_samples) < RV_MIN_RETURNS + 1:
        return None
    log_returns = [
        __import__("math").log(mid_samples[i] / mid_samples[i - 1])
        for i in range(1, len(mid_samples))
        if mid_samples[i] > 0.0 and mid_samples[i - 1] > 0.0
    ]
    if len(log_returns) < RV_MIN_RETURNS:
        return None
    variance = sum(value * value for value in log_returns) / (len(log_returns) - 1)
    return variance**0.5 * (365.25 * 24.0 * 3600.0) ** 0.5


def build_comparison_frame() -> pd.DataFrame:
    catalog = ParquetDataCatalog(CATALOG_PATH)
    start_sec = int(PM_SLUG.rsplit("-", 1)[1])
    end_sec = start_sec + 300
    start_price, start_price_source = resolve_start_price(catalog, PM_SLUG, start_sec)

    okx_samples = sampled_okx_midpoints(catalog, start_sec - max(RV_WINDOW_SECONDS, RV_MIN_RETURNS), end_sec)
    yes_df = sampled_polymarket_books(catalog, PM_YES_INSTRUMENT_ID, start_sec, end_sec + PM_POST_RESOLVE_SECONDS)
    no_df = sampled_polymarket_books(catalog, PM_NO_INSTRUMENT_ID, start_sec, end_sec + PM_POST_RESOLVE_SECONDS)

    merged = yes_df.merge(no_df, on="second", how="outer", suffixes=("_yes", "_no")).sort_values("second")
    merged = merged[(merged["second"] >= start_sec) & (merged["second"] <= end_sec)].copy()

    okx_history: list[float] = []
    fair_probs: list[float | None] = []
    rv_values: list[float | None] = []
    okx_mids: list[float | None] = []

    for second in merged["second"].tolist():
        okx_mid = okx_samples.get(int(second))
        okx_mids.append(okx_mid)
        if okx_mid is not None:
            okx_history.append(okx_mid)
        rv = realized_vol_annualized(okx_history[-(RV_WINDOW_SECONDS + 2) :])
        rv_values.append(rv)
        if okx_mid is None or rv is None:
            fair_probs.append(None)
            continue
        seconds_to_expiry = max(0, end_sec - int(second))
        fair_probs.append(
            fair_probability_up(
                okx_mid,
                start_price,
                seconds_to_expiry,
                realized_vol=rv,
                kurtosis=0.0,
            ),
        )

    merged["timestamp"] = pd.to_datetime(merged["second"], unit="s", utc=True)
    merged["okx_mid"] = okx_mids
    merged["realized_vol"] = rv_values
    merged["fair_prob_up"] = fair_probs
    merged["start_price"] = start_price
    merged["start_price_source"] = start_price_source
    merged["implied_yes_from_no_mid"] = 1.0 - merged["mid_no"]
    merged["yes_model_residual"] = merged["mid_yes"] - merged["fair_prob_up"]
    merged["no_model_residual_as_yes"] = merged["implied_yes_from_no_mid"] - merged["fair_prob_up"]
    return merged


def save_outputs(df: pd.DataFrame) -> tuple[Path, Path]:
    csv_path = OUTPUT_DIR / "pricing_comparison_btc_updown_5m_2026-06-19.csv"
    png_path = OUTPUT_DIR / "pricing_comparison_btc_updown_5m_2026-06-19.png"
    df.to_csv(csv_path, index=False)

    fig, axes = plt.subplots(3, 1, figsize=(14, 10), sharex=True)

    axes[0].plot(df["timestamp"], df["fair_prob_up"], label="Fair P(Up)", linewidth=2)
    axes[0].plot(df["timestamp"], df["mid_yes"], label="YES mid", alpha=0.9)
    axes[0].plot(df["timestamp"], df["implied_yes_from_no_mid"], label="1 - NO mid", alpha=0.9)
    axes[0].set_ylabel("Probability / Price")
    axes[0].set_title("Model Fair Probability vs Polymarket Prices")
    axes[0].legend(loc="best")
    axes[0].grid(True, alpha=0.3)

    axes[1].plot(df["timestamp"], df["yes_model_residual"], label="YES mid - fair", color="tab:red")
    axes[1].plot(df["timestamp"], df["no_model_residual_as_yes"], label="(1-NO mid) - fair", color="tab:orange")
    axes[1].axhline(0.0, color="black", linewidth=1, alpha=0.6)
    axes[1].set_ylabel("Residual")
    axes[1].set_title("Model Residuals")
    axes[1].legend(loc="best")
    axes[1].grid(True, alpha=0.3)

    axes[2].plot(df["timestamp"], df["okx_mid"], label="OKX mid", color="tab:green")
    axes[2].set_ylabel("OKX mid")
    axes[2].set_title("Reference Price")
    axes[2].legend(loc="best")
    axes[2].grid(True, alpha=0.3)

    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(png_path, dpi=160)
    plt.close(fig)
    return csv_path, png_path


def main() -> None:
    df = build_comparison_frame()
    csv_path, png_path = save_outputs(df)
    print(f"rows={len(df)}")
    print(f"csv={csv_path}")
    print(f"png={png_path}")
    if not df.empty:
        print(
            "residual_summary="
            f" yes_mean={df['yes_model_residual'].dropna().mean():.6f}"
            f" yes_abs_mean={df['yes_model_residual'].dropna().abs().mean():.6f}"
            f" no_as_yes_mean={df['no_model_residual_as_yes'].dropna().mean():.6f}"
            f" no_as_yes_abs_mean={df['no_model_residual_as_yes'].dropna().abs().mean():.6f}"
        )


if __name__ == "__main__":
    main()
