from __future__ import annotations

from pathlib import Path
import sys

import matplotlib.pyplot as plt
import numpy as np
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
)


OUTPUT_DIR = Path("/Users/yfclark/pm_backtest/bolt_repro/outputs")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

MAX_LAG_SECONDS = 15


def midpoint(book: OrderBook) -> float | None:
    value = book.midpoint()
    return None if value is None else float(value)


def sample_mid_series(
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
        mid = midpoint(book)
        if mid is None:
            continue
        second = int(delta.ts_event) // 1_000_000_000
        if second in seen_seconds:
            continue
        seen_seconds.add(second)
        rows.append({"second": second, "mid": mid})
    return pd.DataFrame(rows)


def build_dataset() -> pd.DataFrame:
    catalog = ParquetDataCatalog(CATALOG_PATH)
    start_sec = int(PM_SLUG.rsplit("-", 1)[1])
    end_sec = start_sec + 300

    okx = sample_mid_series(catalog, OKX_INSTRUMENT_ID, start_sec - 60, end_sec)
    yes = sample_mid_series(catalog, PM_YES_INSTRUMENT_ID, start_sec, end_sec)
    no = sample_mid_series(catalog, PM_NO_INSTRUMENT_ID, start_sec, end_sec)

    df = yes.merge(no, on="second", how="outer", suffixes=("_yes", "_no"))
    df = df.merge(okx.rename(columns={"mid": "okx_mid"}), on="second", how="left")
    df = df.sort_values("second").reset_index(drop=True)
    df = df[(df["second"] >= start_sec) & (df["second"] <= end_sec)].copy()

    start_price = float(okx[okx["second"] >= start_sec].iloc[0]["mid"])
    df["timestamp"] = pd.to_datetime(df["second"], unit="s", utc=True)
    df["yes_implied_from_no"] = 1.0 - df["mid_no"]
    df["pm_yes_proxy"] = df[["mid_yes", "yes_implied_from_no"]].mean(axis=1)
    df["okx_return"] = (df["okx_mid"] - start_price) / start_price
    df["seconds_to_expiry"] = end_sec - df["second"]
    return df


def normalize_to_probability(series: pd.Series) -> pd.Series:
    centered = series - series.mean()
    scale = centered.std(ddof=0)
    if not np.isfinite(scale) or scale <= 0:
        return pd.Series(np.nan, index=series.index)
    z = centered / scale
    return 1.0 / (1.0 + np.exp(-z))


def lag_correlation(df: pd.DataFrame, max_lag_seconds: int) -> pd.DataFrame:
    rows: list[dict[str, float | int]] = []
    pm = df["pm_yes_proxy"]
    okx = df["okx_prob_proxy"]

    for lag in range(-max_lag_seconds, max_lag_seconds + 1):
        shifted_okx = okx.shift(lag)
        valid = pd.concat([pm, shifted_okx], axis=1).dropna()
        corr = np.nan
        if len(valid) >= 10:
            corr = valid.iloc[:, 0].corr(valid.iloc[:, 1])
        rows.append({"lag_seconds": lag, "correlation": corr, "count": len(valid)})
    return pd.DataFrame(rows)


def segment_correlations(df: pd.DataFrame) -> pd.DataFrame:
    segments = {
        "full_window": df,
        "first_60s": df[df["seconds_to_expiry"] >= 240],
        "middle_180s": df[(df["seconds_to_expiry"] < 240) & (df["seconds_to_expiry"] > 60)],
        "last_60s": df[df["seconds_to_expiry"] <= 60],
    }
    out: list[pd.DataFrame] = []
    for name, segment in segments.items():
        corrs = lag_correlation(segment, MAX_LAG_SECONDS)
        corrs["segment"] = name
        out.append(corrs)
    return pd.concat(out, ignore_index=True)


def save_outputs(df: pd.DataFrame, corr_df: pd.DataFrame) -> tuple[Path, Path, Path]:
    csv_series = OUTPUT_DIR / "lead_lag_series_btc_updown_5m_2026-06-19.csv"
    csv_corr = OUTPUT_DIR / "lead_lag_correlations_btc_updown_5m_2026-06-19.csv"
    png = OUTPUT_DIR / "lead_lag_btc_updown_5m_2026-06-19.png"

    df.to_csv(csv_series, index=False)
    corr_df.to_csv(csv_corr, index=False)

    fig, axes = plt.subplots(2, 1, figsize=(14, 9))

    axes[0].plot(df["timestamp"], df["pm_yes_proxy"], label="PM YES proxy")
    axes[0].plot(df["timestamp"], df["okx_prob_proxy"], label="OKX prob proxy")
    axes[0].set_title("Polymarket vs OKX Probability Proxy")
    axes[0].set_ylabel("Probability-like scale")
    axes[0].legend(loc="best")
    axes[0].grid(True, alpha=0.3)

    for segment, segment_df in corr_df.groupby("segment"):
        axes[1].plot(segment_df["lag_seconds"], segment_df["correlation"], label=segment)
    axes[1].axvline(0, color="black", linewidth=1, alpha=0.5)
    axes[1].set_title("Lag Correlation: corr(PM_t, OKX_{t-lag})")
    axes[1].set_xlabel("Lag seconds")
    axes[1].set_ylabel("Correlation")
    axes[1].legend(loc="best")
    axes[1].grid(True, alpha=0.3)

    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(png, dpi=160)
    plt.close(fig)
    return csv_series, csv_corr, png


def summarize(corr_df: pd.DataFrame) -> str:
    lines: list[str] = []
    for segment, segment_df in corr_df.groupby("segment"):
        valid = segment_df.dropna(subset=["correlation"])
        if valid.empty:
            lines.append(f"{segment}: no_valid_corr")
            continue
        best = valid.loc[valid["correlation"].idxmax()]
        lines.append(
            f"{segment}: best_lag={int(best['lag_seconds'])} best_corr={best['correlation']:.6f} count={int(best['count'])}"
        )
    return " | ".join(lines)


def main() -> None:
    df = build_dataset()
    df["okx_prob_proxy"] = normalize_to_probability(df["okx_return"])
    corr_df = segment_correlations(df)
    csv_series, csv_corr, png = save_outputs(df, corr_df)
    print(f"rows={len(df)}")
    print(f"series_csv={csv_series}")
    print(f"corr_csv={csv_corr}")
    print(f"png={png}")
    print(summarize(corr_df))


if __name__ == "__main__":
    main()
