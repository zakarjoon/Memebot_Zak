"""Bot 1 — Scout (research).

Finds memecoins with real early momentum. Does not judge safety in depth and
never recommends a buy. Outputs a ranked shortlist for the Gatekeeper.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from . import ta
from .config import Section
from .models import UNKNOWN, Record, ScoutBlock, TokenSnapshot, is_unknown, known

BASE58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
FAKE_VERSION = re.compile(r"(\bv\s?2\b|2\.0|\bnew\b|\breal\b|\bofficial\b)", re.IGNORECASE)


@dataclass
class ScoutResult:
    snapshot: TokenSnapshot
    block: ScoutBlock | None
    dropped: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.block is not None


class Scout:
    def __init__(self, cfg: Section):
        self.cfg = cfg
        self.c = cfg.scout

    # ------------------------------------------------------------------ public

    def scan(
        self,
        snapshots: list[TokenSnapshot],
        now: datetime,
        known_symbols: dict[str, str] | None = None,
    ) -> tuple[list[Record], list[ScoutResult]]:
        """Score every snapshot; return (top N records ranked, all results)."""
        results = [self.evaluate(s, now, known_symbols or {}) for s in snapshots]
        passed = [r for r in results if r.passed]
        passed.sort(key=lambda r: (r.block.queue != "priority", -r.block.potential_score))
        records = []
        for r in passed[: self.c.max_candidates]:
            rec = Record.from_snapshot(r.snapshot)
            rec.scout = r.block
            records.append(rec)
        return records, results

    def evaluate(self, s: TokenSnapshot, now: datetime, known_symbols: dict[str, str]) -> ScoutResult:
        drops = self._filters(s, now) + self._instant_drops(s, known_symbols)
        if drops:
            return ScoutResult(s, None, drops)

        factors, unknowns, notes = self._score(s, now)
        score = int(round(sum(factors.values())))
        if score < self.c.watchlist_score:
            return ScoutResult(s, None, [f"potential score {score} < {self.c.watchlist_score}"])

        who = self._who_buys_next(s, now)
        if not who:
            return ScoutResult(s, None, ["cannot answer 'who buys after us?' with evidence"])

        block = ScoutBlock(
            potential_score=score,
            factor_scores={k: int(round(v)) for k, v in factors.items()},
            why_up="; ".join(notes) or "momentum structure intact",
            who_buys_next="; ".join(who),
            metrics={
                "liquidity_usd": s.liquidity_usd,
                "mcap_usd": s.mcap_usd,
                "vol_1h_usd": s.vol_1h_usd,
                "holders": s.holders,
                "age_min": s.age_min,
                "price_usd": s.price_usd,
            },
            sources=list(s.sources),
            queue="priority" if score >= self.c.priority_score else "watchlist",
            unknowns=unknowns,
        )
        return ScoutResult(s, block, [])

    # ------------------------------------------------------------------ filters

    def _filters(self, s: TokenSnapshot, now: datetime) -> list[str]:
        c = self.c
        reasons = []
        checks = [
            ("liquidity_usd", s.liquidity_usd, c.min_liquidity_usd),
            ("age_min", s.age_min, c.min_age_min),
            ("vol_1h_usd", s.vol_1h_usd, c.min_vol_1h_usd),
            ("holders", s.holders, c.min_holders),
        ]
        for name, value, minimum in checks:
            if is_unknown(value):
                reasons.append(f"{name} UNKNOWN (unknown is risk)")
            elif value < minimum:
                reasons.append(f"{name} {value:,.0f} < {minimum:,}")
        if s.age_sec(now) > self.cfg.data.max_age_sec:
            reasons.append(f"data is {s.age_sec(now):.0f}s old (> {self.cfg.data.max_age_sec}s)")

        if is_unknown(s.price_change_24h_pct):
            reasons.append("24h change UNKNOWN (cannot rule out a late >10x)")
        else:
            multiple = 1 + s.price_change_24h_pct / 100
            if multiple > c.max_24h_multiple:
                candles = s.candles_15m or s.candles_5m
                if not ta.held_range(candles, now, c.range_hold_hours, c.range_hold_max_width_pct):
                    reasons.append(
                        f"up {multiple:.1f}x in 24h without a {c.range_hold_hours}h range since — late buyers become exit liquidity"
                    )
        return reasons

    def _instant_drops(self, s: TokenSnapshot, known_symbols: dict[str, str]) -> list[str]:
        reasons = []
        if not BASE58.match(s.address or ""):
            reasons.append("contract address cannot be verified")
        canonical = known_symbols.get(s.symbol.upper())
        if canonical and canonical != s.address:
            reasons.append(f"copycat: ticker {s.symbol} already trades on {canonical[:6]}…")
        if FAKE_VERSION.search(f"{s.name} {s.symbol}"):
            reasons.append("copycat: fake 'v2'/'new'/'real' naming")
        if s.social_paid_or_new_only is True:
            reasons.append("buzz only from paid promoters or brand-new accounts")
        return reasons

    # ------------------------------------------------------------------ scoring

    def _score(self, s: TokenSnapshot, now: datetime) -> tuple[dict[str, float], list[str], list[str]]:
        w = self.c.weights
        factors: dict[str, float] = {}
        unknowns: list[str] = []
        notes: list[str] = []

        # Momentum quality — higher lows, volume with price, no single vertical candle
        c5 = ta.window(s.candles_5m, now, 60)
        c15 = ta.window(s.candles_15m, now, 120)
        if len(c5) >= 6:
            hl = (ta.higher_lows_fraction(c5) + (ta.higher_lows_fraction(c15) if len(c15) >= 6 else 0)) / (
                2 if len(c15) >= 6 else 1
            )
            vfp = min(1.0, max(0.0, (ta.volume_follows_price(c5) - 0.5) / 0.3))
            no_vertical = ta.max_body_pct(c5) < self.c.vertical_candle_pct
            factors["momentum"] = w.momentum * (0.4 * hl + 0.3 * vfp + 0.3 * no_vertical)
            if hl >= 0.6:
                notes.append(f"higher lows on {len(c5)}x5m candles ({hl:.0%})")
            if not no_vertical:
                notes.append("WARNING: vertical candle in last hour")
        else:
            factors["momentum"] = 0
            unknowns.append("momentum (no candles)")

        # Holder growth — steady growth over 1-6h, buyers outnumber sellers
        trend = ta.series_trend(s.holder_history, now, 360) or ta.series_trend(s.holder_history, now, 60)
        steady = ta.steady_growth(s.holder_history, now, 360)
        if trend is None or steady is None:
            factors["holder_growth"] = 0
            unknowns.append("holder_growth (no history)")
        elif trend <= 0:
            factors["holder_growth"] = 0
        else:
            buyers = 1.0 if known(s.buys_1h, s.sells_1h) and s.buys_1h > s.sells_1h else 0.0
            factors["holder_growth"] = w.holder_growth * (0.4 * steady + 0.3 * min(1.0, trend / 20) + 0.3 * buyers)
            notes.append(f"holders +{trend:.0f}% ({steady:.0%} of steps up)")

        # Proven-wallet activity
        if is_unknown(s.proven_wallets_holding):
            factors["proven_wallets"] = 0
            unknowns.append("proven_wallets")
        elif s.proven_wallets_selling is True:
            factors["proven_wallets"] = 0
            notes.append("WARNING: proven wallets are flipping")
        else:
            factors["proven_wallets"] = w.proven_wallets * min(1.0, s.proven_wallets_holding / self.c.proven_wallets_full)

        # Narrative fit
        themes = [t.lower() for t in (self.c.narratives or [])]
        if not themes:
            factors["narrative"] = 0
            unknowns.append("narrative (no current themes configured)")
        else:
            text = f"{s.name} {s.symbol}".lower()
            hit = next((t for t in themes if t in text), None)
            factors["narrative"] = w.narrative if hit else 0
            if hit:
                notes.append(f"rides current theme '{hit}'")

        # Social velocity — many distinct aged accounts, growing
        if not known(s.social_distinct_aged_accounts, s.social_mentions_1h, s.social_mentions_peak_1h):
            factors["social"] = 0
            unknowns.append("social")
        else:
            breadth = min(1.0, s.social_distinct_aged_accounts / 50)
            growing = 1.0 if s.social_mentions_1h >= 0.8 * max(s.social_mentions_peak_1h, 1) else 0.0
            factors["social"] = w.social * (0.5 * breadth + 0.5 * growing)

        # Liquidity depth — >= 10% of market cap and deepening
        if known(s.liquidity_usd, s.mcap_usd) and s.mcap_usd > 0:
            ratio = s.liquidity_usd / s.mcap_usd * 100
            liq_trend = ta.series_trend(s.liquidity_history, now, 60)
            deepening = 1.0 if (liq_trend is not None and liq_trend > 0) else 0.0
            factors["liquidity"] = w.liquidity * (
                (2 / 3) * min(1.0, ratio / self.c.min_liq_to_mcap_pct) + (1 / 3) * deepening
            )
        else:
            factors["liquidity"] = 0
            unknowns.append("liquidity depth")

        return factors, unknowns, notes

    def _who_buys_next(self, s: TokenSnapshot, now: datetime) -> list[str]:
        """Evidence of the next buyers. Empty list means drop the token."""
        ev = []
        h1 = ta.series_trend(s.holder_history, now, 60)
        if h1 is not None and h1 > 0:
            ev.append(f"holders +{h1:.1f}% in the last hour")
        if known(s.buys_1h, s.sells_1h) and s.buys_1h > s.sells_1h:
            ev.append(f"{s.buys_1h} buys vs {s.sells_1h} sells in 1h")
        if not is_unknown(s.proven_wallets_holding) and s.proven_wallets_holding > 0 and s.proven_wallets_selling is not True:
            ev.append(f"{s.proven_wallets_holding} tracked wallets holding")
        trending = [src for src in s.sources if "trending" in src]
        if trending:
            ev.append(f"listed on {len(trending)} trending list(s)")
        # buy pressure alone is not enough — need a second, independent signal
        return ev if len(ev) >= 2 else []


__all__ = ["Scout", "ScoutResult", "UNKNOWN"]
