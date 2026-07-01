"""
startPrice logic ported/adapted from /Users/yfclark/polymarket_stale_quote/start_price.py
and start_price_client.py

This is how they get the startPrice (openPrice / k_price) of the underlying for the binary updown event:
- Fetch the Polymarket event page for the slug.
- Parse __NEXT_DATA__ JSON.
- Find the event slug query to get startTime (as unix sec).
- Find the crypto-prices query for that start time, take "openPrice" as the start price of BTC (or asset) at the event start.
- This startPrice is used as anchor for binary option pricing, price agreement, fair prob, EV calc in the strategy (see bolt_v3_taker_updown_signal.price_agreement_corr(observed, anchor), and in binary_oracle_edge_taker for price_to_beat / interval_open).

For the repro in Nautilus pyO3 strategy, we use this to get consistent startPrice for each updown slug at its start_sec (from slug).
For historical backtest, fetching the page (if data is still there) gives the correct historical openPrice for that event.
"""

import json
import math
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote

import requests

ONE_BILLION = 1_000_000_000

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(?P<body>.*?)</script>',
    re.DOTALL,
)

def _parse_iso8601_to_epoch_s(value: object) -> Optional[int]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())

def _as_finite_float(value: object) -> Optional[float]:
    if isinstance(value, (int, float)):
        fv = float(value)
        if math.isfinite(fv):
            return fv
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            fv = float(text)
        except ValueError:
            return None
        if math.isfinite(fv):
            return fv
    return None

def _extract_next_data_json(html: str) -> Optional[dict[str, Any]]:
    match = _NEXT_DATA_RE.search(html)
    if match is None:
        return None
    try:
        payload = json.loads(match.group("body"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None

@dataclass(frozen=True)
class StartPriceSnapshot:
    slug: str
    start_sec: int
    k_price: float
    source: str

def extract_start_price_snapshot(html: str, slug: str) -> Optional[StartPriceSnapshot]:
    payload = _extract_next_data_json(html)
    if payload is None:
        return None
    dehydrated = (
        payload.get("props", {})
        .get("pageProps", {})
        .get("dehydratedState", {})
    )
    queries_raw = dehydrated.get("queries")
    if not isinstance(queries_raw, list):
        return None
    queries = [q for q in queries_raw if isinstance(q, dict)]
    slug_lc = slug.strip().lower()

    event_start_sec: Optional[int] = None

    # Resolve event start from event query for this slug.
    for query in queries:
        query_key = query.get("queryKey")
        if not isinstance(query_key, list) or len(query_key) < 2:
            continue
        if query_key[0] != "/api/event/slug":
            continue
        if str(query_key[1]).strip().lower() != slug_lc:
            continue
        state = query.get("state")
        if not isinstance(state, dict):
            continue
        data = state.get("data")
        if not isinstance(data, dict):
            continue
        start_time = data.get("startTime")
        if isinstance(start_time, str) and start_time.strip():
            event_start_sec = _parse_iso8601_to_epoch_s(start_time.strip())
            break

    if event_start_sec is None:
        return None

    for query in queries:
        query_key = query.get("queryKey")
        if not isinstance(query_key, list) or len(query_key) < 4:
            continue
        if str(query_key[0]).strip().lower() != "crypto-prices":
            continue
        if str(query_key[1]).strip().lower() != "price":
            continue
        key_start_sec = _parse_iso8601_to_epoch_s(query_key[3])
        if key_start_sec is None or key_start_sec != event_start_sec:
            continue
        state = query.get("state")
        if not isinstance(state, dict):
            continue
        data = state.get("data")
        if not isinstance(data, dict):
            continue
        open_price = _as_finite_float(data.get("openPrice"))
        if open_price is None:
            continue
        return StartPriceSnapshot(
            slug=slug,
            start_sec=event_start_sec,
            k_price=open_price,
            source="crypto_prices_open",
        )

    return None

def _fetch_event_page_html(slug: str, timeout_s: float) -> str:
    url = f"https://polymarket.com/event/{quote(slug, safe='')}"
    response = requests.get(
        url,
        timeout=timeout_s,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; polymarket-stale-quote/1.0)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    response.raise_for_status()
    return response.text

@dataclass
class _CacheEntry:
    cached_ns: int
    snapshot: StartPriceSnapshot

class PolymarketStartPriceProvider:
    def __init__(
        self,
        request_timeout_secs: float,
        max_retries: int,
        retry_backoff_secs: float,
        cache_ttl_secs: int,
        cache_max_items: int,
        fetch_html: Any = None,
    ) -> None:
        self._request_timeout_secs = max(1.0, float(request_timeout_secs))
        self._max_retries = max(1, int(max_retries))
        self._retry_backoff_secs = max(0.0, float(retry_backoff_secs))
        self._cache_ttl_secs = max(1, int(cache_ttl_secs))
        self._cache_max_items = max(8, int(cache_max_items))
        self._fetch_html = fetch_html or _fetch_event_page_html
        self._cache: OrderedDict[str, _CacheEntry] = OrderedDict()

    def _fetch_snapshot(self, slug: str) -> Optional[StartPriceSnapshot]:
        html: Optional[str] = None
        for attempt in range(1, self._max_retries + 1):
            try:
                html = self._fetch_html(slug, self._request_timeout_secs)
            except requests.RequestException:
                html = None
            if html is not None:
                break
            if attempt < self._max_retries and self._retry_backoff_secs > 0.0:
                time.sleep(self._retry_backoff_secs * attempt)
        if html is None:
            return None
        return extract_start_price_snapshot(html, slug)

    def resolve_by_slug_start_sec(self, slug: str, start_sec: int) -> Optional[StartPriceSnapshot]:
        now_ns = time.time_ns()
        target_slug = str(slug).strip()
        if not target_slug:
            return None

        item = self._cache.get(target_slug)
        if item is not None:
            cache_fresh = now_ns - item.cached_ns <= self._cache_ttl_secs * ONE_BILLION
            if cache_fresh and int(item.snapshot.start_sec) == int(start_sec):
                self._cache.move_to_end(target_slug)
                return item.snapshot
            self._cache.pop(target_slug, None)

        snapshot = self._fetch_snapshot(target_slug)
        if snapshot is None:
            return None
        if int(snapshot.start_sec) != int(start_sec):
            return None

        self._cache[target_slug] = _CacheEntry(cached_ns=now_ns, snapshot=snapshot)
        self._cache.move_to_end(target_slug)
        while len(self._cache) > self._cache_max_items:
            self._cache.popitem(last=False)
        return snapshot

# Example usage for a slug:
# provider = PolymarketStartPriceProvider(10.0, 3, 0.5, 3600, 100)
# snapshot = provider.resolve_by_slug_start_sec("btc-updown-5m-1781827200", 1781827200)
# if snapshot:
#     start_price = snapshot.k_price
