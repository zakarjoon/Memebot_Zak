"""Small, deterministic chart helpers shared by the bots."""
from __future__ import annotations

from datetime import datetime, timedelta

from .models import Candle


def pct_change(a: float, b: float) -> float:
    """Percent change from a to b."""
    if a == 0:
        return 0.0
    return (b - a) / a * 100.0


def window(candles: list[Candle], now: datetime, minutes: float) -> list[Candle]:
    start = now - timedelta(minutes=minutes)
    return [c for c in candles if c.ts >= start]


def higher_lows_fraction(candles: list[Candle], pivot: int = 3) -> float:
    """Share of successive local lows that are higher than the previous one (0-1)."""
    lows = []
    for i in range(0, len(candles), pivot):
        chunk = candles[i : i + pivot]
        if chunk:
            lows.append(min(c.low for c in chunk))
    if len(lows) < 3:
        return 0.0
    ups = sum(1 for a, b in zip(lows, lows[1:]) if b > a)
    return ups / (len(lows) - 1)


def max_body_pct(candles: list[Candle]) -> float:
    if not candles:
        return 0.0
    return max(abs(pct_change(c.open, c.close)) for c in candles)


def volume_follows_price(candles: list[Candle]) -> float:
    """Share of rising-volume moves that happen on up candles (0-1)."""
    if len(candles) < 4:
        return 0.0
    up_vol = sum(c.volume_usd for c in candles if c.close >= c.open)
    total = sum(c.volume_usd for c in candles) or 1.0
    return up_vol / total


def range_width_pct(candles: list[Candle]) -> float:
    if not candles:
        return float("inf")
    hi = max(c.high for c in candles)
    lo = min(c.low for c in candles)
    return pct_change(lo, hi)


def held_range(candles: list[Candle], now: datetime, hours: float, max_width_pct: float) -> bool:
    w = window(candles, now, hours * 60)
    span_ok = bool(w) and (now - w[0].ts) >= timedelta(hours=hours) - timedelta(minutes=15)
    return span_ok and range_width_pct(w) <= max_width_pct


def series_trend(series: list[tuple[datetime, float]], now: datetime, minutes: float) -> float | None:
    """Percent change of a time series over the last ``minutes``; None if not enough data."""
    pts = [(t, v) for t, v in series if t >= now - timedelta(minutes=minutes)]
    if len(pts) < 2 or pts[0][1] == 0:
        return None
    covered = (pts[-1][0] - pts[0][0]).total_seconds() / 60
    if covered < minutes * 0.5:
        return None
    return pct_change(pts[0][1], pts[-1][1])


def steady_growth(series: list[tuple[datetime, float]], now: datetime, minutes: float) -> float | None:
    """Share of steps that grew (0-1) over the window; None if not enough data."""
    pts = [v for t, v in series if t >= now - timedelta(minutes=minutes)]
    if len(pts) < 4:
        return None
    ups = sum(1 for a, b in zip(pts, pts[1:]) if b > a)
    return ups / (len(pts) - 1)


def last_higher_low(candles: list[Candle]) -> float | None:
    """Most recent swing low that is higher than the swing low before it."""
    swings = []
    for i in range(1, len(candles) - 1):
        if candles[i].low <= candles[i - 1].low and candles[i].low <= candles[i + 1].low:
            swings.append(candles[i].low)
    for a, b in zip(reversed(swings[:-1]), reversed(swings)):
        if b > a:
            return b
    return None


def lower_high_lower_low(candles: list[Candle]) -> bool:
    """True if the last closed candle printed both a lower high and a lower low.

    Candle lists only ever hold *closed* candles.
    """
    if len(candles) < 2:
        return False
    prev, last = candles[-2], candles[-1]
    return last.high < prev.high and last.low < prev.low


def aggregate(candles: list[Candle], minutes: int, now: datetime, base_minutes: int = 5) -> list[Candle]:
    """Roll small closed candles up into ``minutes`` candles aligned to UTC period
    boundaries. Only periods that have fully closed by ``now`` are returned."""
    buckets: dict[datetime, list[Candle]] = {}
    for c in candles:
        epoch = int(c.ts.timestamp())
        start = epoch - epoch % (minutes * 60)
        buckets.setdefault(datetime.fromtimestamp(start, tz=c.ts.tzinfo), []).append(c)
    out = []
    for key in sorted(buckets):
        if key + timedelta(minutes=minutes) > now:
            continue
        g = sorted(buckets[key], key=lambda c: c.ts)
        out.append(Candle(key, g[0].open, max(c.high for c in g), min(c.low for c in g), g[-1].close, sum(c.volume_usd for c in g)))
    return out
