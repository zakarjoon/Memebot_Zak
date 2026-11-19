"""A deterministic simulated Solana memecoin market.

Used for tests and for a paper-trading demo when no network is available. Each
token follows a scripted price/volume/holder path with seeded noise, and some
carry traps (rug pull, honeypot, live mint authority, whale wallet, copycat).
This is NOT a market model and says nothing about real expected value.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from ..broker import pool_impact_pct
from ..models import UNKNOWN, Candle, Holder, MarketContext, SafetyReport, TokenSnapshot

START = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def fake_address(seed: str) -> str:
    rng = random.Random(seed)
    return "".join(rng.choice(B58) for _ in range(44))


@dataclass
class Segment:
    minutes: int
    mult: float  # price multiple reached at the end of the segment
    vol_per_min: float  # USD volume per minute
    holders_per_min: float
    buy_ratio: float = 1.2  # buys / sells
    noise: float = 0.01  # per-minute log-price noise


@dataclass
class SimToken:
    symbol: str
    name: str
    p0: float
    liquidity0: float
    supply: float
    holders0: int
    segments: list[Segment]
    age_at_start: int  # minutes since launch when the sim clock starts
    seed: int = 1
    safety: dict[str, Any] = field(default_factory=dict)
    signals: dict[str, Any] = field(default_factory=dict)  # proven wallets / social
    events: list[tuple[int, str]] = field(default_factory=list)  # (sim minute, event)
    address: str = ""

    def __post_init__(self):
        self.address = self.address or fake_address(self.symbol + str(self.seed))
        self._build()

    def _build(self) -> None:
        rng = random.Random(self.seed)
        price, vol, holders, ratio = [], [], [], []
        p, h, dev = self.p0, float(self.holders0), 0.0
        for seg in self.segments:
            step = math.log(seg.mult) / seg.minutes
            for _ in range(seg.minutes):
                dev = 0.85 * dev + rng.gauss(0, seg.noise)  # mean-reverting noise around the scripted path
                p *= math.exp(step)
                price.append(p * math.exp(dev))
                h += seg.holders_per_min * (0.5 + rng.random())
                vol.append(seg.vol_per_min * (0.6 + 0.8 * rng.random()))
                holders.append(h)
                ratio.append(seg.buy_ratio * (0.9 + 0.2 * rng.random()))
        self.price, self.vol, self.holders, self.ratio = price, vol, holders, ratio
        self.liq = [self.liquidity0 * math.sqrt(x / self.p0) for x in price]

    def apply_events(self, launch_minute_of: int) -> None:
        for sim_minute, event in self.events:
            t = launch_minute_of + sim_minute
            if event == "rug":
                for i in range(t, len(self.price)):
                    self.price[i] *= 0.04
                    self.liq[i] *= 0.03
                    self.ratio[i] = 0.1


class SimulatedMarket:
    name = "simulated"

    def __init__(self, tokens: list[SimToken] | None = None, start: datetime = START, canonical: dict[str, str] | None = None):
        self.start = start
        self.minute = 0
        self.tokens = {t.address: t for t in (tokens if tokens is not None else default_tokens())}
        for t in self.tokens.values():
            t.apply_events(t.age_at_start)
        self.canonical = canonical if canonical is not None else {
            t.symbol.upper(): t.address for t in self.tokens.values() if t.signals.get("canonical")
        }

    # ------------------------------------------------------------------ clock

    def now(self) -> datetime:
        return self.start + timedelta(minutes=self.minute)

    def advance(self, minutes: int = 1) -> None:
        self.minute += minutes

    def _t(self, tok: SimToken) -> int:
        return min(tok.age_at_start + self.minute, len(tok.price) - 1)

    async def close(self) -> None:
        return None

    # ------------------------------------------------------------------ provider API

    async def discover(self) -> tuple[list[str], dict[str, str]]:
        return [a for a, t in self.tokens.items() if self._t(t) > 0], dict(self.canonical)

    async def snapshots(self, addresses: list[str], full: bool = True) -> dict[str, TokenSnapshot]:
        return {a: self._snapshot(self.tokens[a], full) for a in addresses if a in self.tokens}

    def _candles(self, tok: SimToken, t: int, minutes: int, lookback: int) -> list[Candle]:
        out = []
        now = self.now()
        launch = now - timedelta(minutes=t)
        first = max(0, t - lookback)
        first -= first % minutes
        for s in range(first, t - minutes + 1, minutes):
            seg = tok.price[s : s + minutes]
            prev = tok.price[s - 1] if s > 0 else tok.p0
            if len(seg) < minutes:
                break
            out.append(Candle(launch + timedelta(minutes=s), prev, max([prev] + seg), min([prev] + seg), seg[-1], sum(tok.vol[s : s + minutes])))
        return out

    def _snapshot(self, tok: SimToken, full: bool) -> TokenSnapshot:
        t = self._t(tok)
        now = self.now()
        price = tok.price[t]
        tx_1h = sum(tok.vol[max(0, t - 60) : t]) / 150
        r = tok.ratio[t]
        sig = tok.signals
        snap = TokenSnapshot(
            chain="solana",
            address=tok.address,
            symbol=tok.symbol,
            name=tok.name,
            pair_address=fake_address("pool" + tok.address),
            data_timestamp=now,
            price_usd=price,
            liquidity_usd=tok.liq[t],
            mcap_usd=price * tok.supply,
            vol_1h_usd=sum(tok.vol[max(0, t - 60) : t]),
            vol_24h_usd=sum(tok.vol[max(0, t - 1440) : t]),
            holders=tok.holders[t],
            age_min=t,
            price_change_24h_pct=(price / (tok.price[t - 1440] if t >= 1440 else tok.p0) - 1) * 100,
            buys_1h=int(tx_1h * r / (1 + r)),
            sells_1h=int(tx_1h / (1 + r)),
            unique_traders_1h=int(tx_1h * 0.55),
            proven_wallets_holding=sig.get("proven_wallets", UNKNOWN),
            proven_wallets_selling=sig.get("proven_selling", UNKNOWN),
            social_mentions_1h=sig.get("mentions", UNKNOWN),
            social_mentions_peak_1h=sig.get("mentions_peak", UNKNOWN),
            social_distinct_aged_accounts=sig.get("aged_accounts", UNKNOWN),
            social_paid_or_new_only=sig.get("paid_only", UNKNOWN),
            sources=["sim", "sim_trending"] if sig.get("trending") else ["sim"],
        )
        if full:
            snap.candles_5m = self._candles(tok, t, 5, 8 * 60)
            snap.candles_15m = self._candles(tok, t, 15, 12 * 60)
            snap.candles_1h = self._candles(tok, t, 60, 24 * 60)
            snap.holder_history = [(now - timedelta(minutes=t - i), tok.holders[i]) for i in range(max(0, t - 360), t + 1, 5)]
            snap.liquidity_history = [(now - timedelta(minutes=t - i), tok.liq[i]) for i in range(max(0, t - 60), t + 1)]
            snap.buy_sell_history = [(now - timedelta(minutes=t - i), tok.ratio[i]) for i in range(max(0, t - 60), t + 1, 5)]
        return snap

    async def safety(self, snap: TokenSnapshot) -> SafetyReport:
        tok = self.tokens[snap.address]
        s = dict(DEFAULT_SAFETY)
        s.update(tok.safety)
        for m, e in tok.events:
            if self.minute >= m and e.startswith("set:"):
                key, val = e[4:].split("=")
                s[key] = {"true": True, "false": False}.get(val, val if not val.replace(".", "").isdigit() else float(val))
        holders = [Holder(fake_address(f"h{tok.symbol}{i}"), pct, False) for i, pct in enumerate(s.pop("holder_pcts"))]
        holders.insert(0, Holder(fake_address("pool" + tok.address), 22.0, True, "AMM"))
        rep = SafetyReport(data_timestamp=self.now(), top_holders=holders, sources=["sim"], **s)
        return rep

    async def market_context(self) -> MarketContext:
        return MarketContext(base_coin_trend_24h_pct=1.0, meme_sector_volume_trend_pct=5.0, hour_utc=self.now().hour)

    async def quote_impacts(self, snap: TokenSnapshot, usd: float):
        tok = self.tokens[snap.address]
        liq = tok.liq[self._t(tok)]
        sell_ok = tok.safety.get("sell_simulation_ok", True)
        return pool_impact_pct(usd, liq), (pool_impact_pct(usd, liq) if sell_ok else 100.0)

    def liquidity(self, address: str) -> float:
        tok = self.tokens[address]
        return tok.liq[self._t(tok)]


DEFAULT_SAFETY: dict[str, Any] = dict(
    sell_simulation_ok=True,
    sell_tax_pct=0.0,
    tax_mutable=False,
    mint_authority_active=False,
    freeze_authority_active=False,
    lp_locked_pct=100.0,
    lp_lock_days_remaining=float("inf"),
    upgradeable=False,
    deployer_address="DeployerSim",
    deployer_age_days=40,
    deployer_rugged_before=False,
    deployer_moving_funds=False,
    impersonation=False,
    insider_supply_pct=3.0,
    dev_holding_pct=2.0,
    insiders_selling=False,
    copied_website=False,
    bot_followers=False,
    holder_pcts=[4.0, 3.0, 2.5, 2.0, 1.8, 1.5, 1.2, 1.0, 0.8, 0.7],
)

GOOD_SIGNALS = dict(proven_wallets=2, proven_selling=False, mentions=420, mentions_peak=450, aged_accounts=70, paid_only=False, trending=True)


def base_path(tail: list[Segment]) -> list[Segment]:
    """Launch pump -> pullback -> 3h base -> volume expansion, then the scripted tail."""
    return [
        Segment(40, 4.0, 3000, 6, 1.6, 0.015),
        Segment(20, 0.75, 1500, 2, 0.9, 0.01),
        Segment(180, 1.0, 600, 1.2, 1.15, 0.006),
        Segment(15, 1.08, 2200, 3, 1.5, 0.004),
    ] + tail


def default_tokens() -> list[SimToken]:
    age = 250  # starts during the volume-expansion candle(s) after the base
    return [
        SimToken("PURRAI", "Purr AI Cat", 0.0001, 60_000, 1e10, 400,
                 base_path([Segment(150, 3.4, 2500, 3, 1.4, 0.004), Segment(120, 2.6, 2000, 2, 1.3, 0.004),
                            Segment(150, 0.6, 900, -1, 0.7, 0.006), Segment(400, 1.0, 300, 0, 1.0, 0.004)]),
                 age_at_start=age, seed=7, signals={**GOOD_SIGNALS, "canonical": True}),
        SimToken("FROGLORD", "Frog Lord", 0.0002, 60_000, 5e9, 450,
                 base_path([Segment(30, 1.03, 1500, 1, 1.2, 0.004), Segment(900, 1.0, 800, 0.5, 1.0, 0.004)]),
                 age_at_start=age, seed=11, signals=GOOD_SIGNALS, events=[(45, "rug")]),
        SimToken("DOGEGPT", "Doge GPT", 0.0003, 60_000, 3e9, 420,
                 base_path([Segment(60, 0.80, 900, -0.5, 0.8, 0.004), Segment(900, 0.9, 400, -0.2, 0.9, 0.004)]),
                 age_at_start=age, seed=21, signals=GOOD_SIGNALS),
        SimToken("HONEYCAT", "Honey Cat", 0.0001, 60_000, 1e10, 420,
                 base_path([Segment(1000, 1.5, 1200, 1, 1.5, 0.004)]),
                 age_at_start=age, seed=31, signals=GOOD_SIGNALS, safety={"sell_simulation_ok": False}),
        SimToken("MINTDOG", "Mint Dog", 0.0001, 60_000, 1e10, 420,
                 base_path([Segment(1000, 1.5, 1200, 1, 1.5, 0.004)]),
                 age_at_start=age, seed=41, signals=GOOD_SIGNALS, safety={"mint_authority_active": True}),
        SimToken("WHALEAI", "Whale AI", 0.0001, 60_000, 1e10, 420,
                 base_path([Segment(1000, 1.5, 1200, 1, 1.5, 0.004)]),
                 age_at_start=age, seed=51, signals=GOOD_SIGNALS,
                 safety={"holder_pcts": [24.0, 3.0, 2.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0]}),
        SimToken("TINYCAT", "Tiny Cat", 0.0001, 8_000, 1e9, 420,
                 base_path([Segment(1000, 1.5, 200, 1, 1.5, 0.004)]),
                 age_at_start=age, seed=61, signals=GOOD_SIGNALS),
        SimToken("PURRAI", "Purr AI Cat", 0.0001, 60_000, 1e10, 420,
                 base_path([Segment(1000, 1.5, 1200, 1, 1.5, 0.004)]),
                 age_at_start=age, seed=71, signals=GOOD_SIGNALS),  # copycat ticker on a new contract
        SimToken("QUIETAI", "Quiet AI", 0.0001, 60_000, 1e10, 420,
                 base_path([Segment(1000, 1.5, 1200, 1, 1.5, 0.004)]),
                 age_at_start=age, seed=81, signals={}),  # no social / wallet data: free-data reality
    ]
