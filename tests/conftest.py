from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from memebot.config import load_config
from memebot.models import Candle, Holder, MarketContext, SafetyReport, TokenSnapshot

NOW = datetime(2026, 11, 15, 21, 30, tzinfo=timezone.utc)
ADDR = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"


@pytest.fixture
def cfg():
    return load_config()


def candles(prices: list[float], start: datetime, minutes: int = 5, vols: list[float] | None = None) -> list[Candle]:
    out = []
    prev = prices[0]
    for i, p in enumerate(prices):
        v = vols[i] if vols else 1000.0
        out.append(Candle(start + timedelta(minutes=minutes * i), prev, max(prev, p) * 1.002, min(prev, p) * 0.998, p, v))
        prev = p
    return out


def range_base_candles(now: datetime = NOW) -> tuple[list[Candle], float]:
    """Pump to 1.0, base 0.70-0.76 for 2.5h, then one expansion candle to 0.765."""
    n_pump, n_base = 20, 30
    pump = [0.3 + 0.7 * i / (n_pump - 1) for i in range(n_pump)]
    base = [0.73 + (0.025 if i % 2 else -0.025) for i in range(n_base)]
    last = [0.765]
    prices = pump + base + last
    vols = [3000.0] * n_pump + [800.0] * n_base + [3000.0]
    start = now - timedelta(minutes=5 * len(prices))
    return candles(prices, start, 5, vols), 0.765


def snapshot(**kw) -> TokenSnapshot:
    base = dict(
        chain="solana", address=ADDR, symbol="EXAMPLE", name="Example Cat", pair_address="Pool1111111111111111111111111111111111111111",
        data_timestamp=NOW, price_usd=0.765, liquidity_usd=120_000, mcap_usd=900_000, vol_1h_usd=60_000,
        vol_24h_usd=600_000, holders=1450, age_min=210, price_change_24h_pct=150.0, buys_1h=500, sells_1h=300,
        unique_traders_1h=450,
    )
    base.update(kw)
    s = TokenSnapshot(**base)
    if "candles_5m" not in kw:
        s.candles_5m, _ = range_base_candles()
    if "holder_history" not in kw:
        s.holder_history = [(NOW - timedelta(minutes=300 - 10 * i), 1000 + 15 * i) for i in range(31)]
    if "liquidity_history" not in kw:
        s.liquidity_history = [(NOW - timedelta(minutes=60 - 5 * i), 110_000 + 800 * i) for i in range(13)]
    return s


def safety(**kw) -> SafetyReport:
    base = dict(
        data_timestamp=NOW, sell_simulation_ok=True, sell_tax_pct=0.0, tax_mutable=False, mint_authority_active=False,
        freeze_authority_active=False, lp_locked_pct=100.0, lp_lock_days_remaining=float("inf"), upgradeable=False,
        deployer_address="Dev", deployer_age_days=60, deployer_rugged_before=False, deployer_moving_funds=False,
        impersonation=False, insider_supply_pct=2.0, dev_holding_pct=2.0, insiders_selling=False,
        copied_website=False, bot_followers=False,
        top_holders=[Holder("LPpool", 25.0, True, "AMM")] + [Holder(f"w{i}", 2.0) for i in range(10)],
    )
    base.update(kw)
    return SafetyReport(**base)


def market(**kw) -> MarketContext:
    base = dict(base_coin_trend_24h_pct=2.0, meme_sector_volume_trend_pct=5.0, hour_utc=21)
    base.update(kw)
    return MarketContext(**base)
