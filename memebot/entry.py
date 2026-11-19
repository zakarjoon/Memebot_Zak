"""Bot 3 — Entry (where to enter and how much to risk).

For PASS and CAUTION tokens only. "No entry yet" is a valid answer and often
the best one.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from . import ta
from .config import Section
from .models import EntryBlock, Record, TokenSnapshot, iso, is_unknown, known


@dataclass
class Setup:
    name: str
    zone: tuple[float, float]
    stop: float
    target: float
    note: str

    @property
    def mid(self) -> float:
        return (self.zone[0] + self.zone[1]) / 2

    @property
    def stop_distance(self) -> float:
        return (self.mid - self.stop) / self.mid

    @property
    def reward_to_risk(self) -> float:
        risk = self.mid - self.stop
        return (self.target - self.mid) / risk if risk > 0 else 0.0


def position_size(
    capital: float,
    risk_per_trade: float,
    multiplier: float,
    stop_distance: float,
    pool_liquidity: float,
    room_left: float,
    max_position_frac: float = 0.02,
    pool_share: float = 0.01,
) -> tuple[float, dict[str, float]]:
    """size = min(C*r*m/d, C*0.02*m, L*0.01, R)."""
    limits = {
        "risk_based": capital * risk_per_trade * multiplier / stop_distance if stop_distance > 0 else 0.0,
        "position_cap": capital * max_position_frac * multiplier,
        "pool_share": pool_liquidity * pool_share,
        "exposure_room": room_left,
    }
    return max(0.0, min(limits.values())), limits


class Entry:
    def __init__(self, cfg: Section):
        self.cfg = cfg
        self.c = cfg.entry

    # ------------------------------------------------------------------ public

    def plan(
        self,
        record: Record,
        snap: TokenSnapshot,
        now: datetime,
        *,
        capital: float,
        room_left: float,
        size_factor: float = 1.0,
    ) -> EntryBlock:
        """Draft a plan from chart structure. Quotes are checked afterwards in ``preflight``."""
        gk = record.gatekeeper
        reasons: list[str] = []

        if gk is None or gk.verdict not in ("PASS", "CAUTION"):
            return self._no_entry(record, [f"Gatekeeper verdict is {gk.verdict if gk else 'missing'} — Entry never overrides a REJECT"])

        gk_age = (now - datetime.fromisoformat(gk.checked_at.replace("Z", "+00:00"))).total_seconds() / 60
        if gk_age > self.c.max_gatekeeper_age_min:
            reasons.append(f"Gatekeeper verdict is {gk_age:.0f} min old (> {self.c.max_gatekeeper_age_min})")
        data_age = snap.age_sec(now) / 60
        if data_age > self.c.max_data_age_min:
            reasons.append(f"market data is {data_age:.1f} min old (> {self.c.max_data_age_min})")
        if not known(snap.price_usd, snap.liquidity_usd):
            reasons.append("price or liquidity UNKNOWN")
        if reasons:
            return self._no_entry(record, reasons)

        reasons += self._do_not_enter_market(snap, now)
        if reasons:
            return self._no_entry(record, reasons)

        min_rr = self.c.min_reward_to_risk[gk.tier]
        setups = self.detect_setups(snap, now)
        if not setups:
            return self._no_entry(record, ["no setup fits (pullback / breakout-retest / range base)"])

        valid: list[Setup] = []
        for s in setups:
            problems = []
            if s.stop_distance * 100 > self.c.max_stop_distance_pct:
                problems.append(f"{s.name}: stop {s.stop_distance:.0%} below entry (> {self.c.max_stop_distance_pct}%) — too loose")
            if s.reward_to_risk < min_rr:
                problems.append(f"{s.name}: reward-to-risk {s.reward_to_risk:.2f} < {min_rr} for tier {gk.tier}")
            if s.stop_distance <= 0:
                problems.append(f"{s.name}: stop is not below entry")
            if problems:
                reasons += problems
            else:
                valid.append(s)
        if not valid:
            return self._no_entry(record, reasons)

        best = max(valid, key=lambda s: s.reward_to_risk)
        size, limits = position_size(
            capital=capital,
            risk_per_trade=self.c.risk_per_trade_pct / 100,
            multiplier=gk.size_multiplier,
            stop_distance=best.stop_distance,
            pool_liquidity=snap.liquidity_usd,
            room_left=room_left,
            max_position_frac=self.cfg.risk.max_position_pct / 100,
            pool_share=self.c.max_pool_share_pct / 100,
        )
        size = round(size * size_factor, 2)
        if size < 1:
            return self._no_entry(record, [f"size ${size:.2f} too small (limits: {limits})"])

        split = self.c.tranche_split_pct
        block = EntryBlock(
            decision="ENTER",
            setup=best.name,
            entry_zone=[_r(best.zone[0]), _r(best.zone[1])],
            size_usd=size,
            tranches=[
                {"pct": split[0], "trigger": "price in zone"},
                {"pct": split[1], "trigger": f"holds zone {self.c.tranche2_confirm_min} min or prints a higher low"},
            ],
            stop_price=_r(best.stop),
            invalidation=f"5-min close below {_r(best.stop)}",
            first_target=_r(best.target),
            reward_to_risk=round(best.reward_to_risk, 2),
            max_slippage_pct=min(self.c.default_slippage_pct, self.c.max_slippage_pct),
            expires_at=iso(now + timedelta(minutes=self.c.plan_expiry_min)),
            reasons=[best.note],
            size_limits={k: round(v, 2) for k, v in limits.items()},
        )
        record.entry = block
        return block

    def preflight(self, record: Record, buy_impact_pct, sell_impact_pct) -> EntryBlock:
        """Quote checks for the full size: buy impact <= 1%, simulated sell <= 3%."""
        block = record.entry
        if block is None or block.decision != "ENTER":
            return block
        problems = []
        if is_unknown(buy_impact_pct):
            problems.append("buy quote UNKNOWN")
        elif buy_impact_pct > self.c.max_buy_impact_pct:
            problems.append(f"buy price impact {buy_impact_pct:.2f}% > {self.c.max_buy_impact_pct}%")
        if is_unknown(sell_impact_pct):
            problems.append("sell simulation UNKNOWN — if you cannot get out, do not get in")
        elif sell_impact_pct > self.c.max_sell_impact_pct:
            problems.append(f"simulated sell impact {sell_impact_pct:.2f}% > {self.c.max_sell_impact_pct}% — cannot get out")
        if problems:
            return self._no_entry(record, problems)
        block.reasons.append(f"preflight ok: buy impact {buy_impact_pct:.2f}%, sell impact {sell_impact_pct:.2f}%")
        return block

    # ------------------------------------------------------------------ rules

    def _do_not_enter_market(self, snap: TokenSnapshot, now: datetime) -> list[str]:
        reasons = []
        last15 = ta.window(snap.candles_5m, now, 15)
        if last15:
            rise = ta.pct_change(min(c.low for c in last15), snap.price_usd)
            if rise > self.c.max_15m_rise_pct:
                reasons.append(f"price up {rise:.0f}% in 15 min — wait for structure")
        if known(snap.buys_1h, snap.sells_1h) and snap.buys_1h < snap.sells_1h:
            reasons.append(f"buy/sell ratio negative ({snap.buys_1h}/{snap.sells_1h})")
        holder_trend = ta.series_trend(snap.holder_history, now, 30)
        if holder_trend is not None and holder_trend < 0:
            reasons.append(f"holder count falling ({holder_trend:.1f}% in 30 min)")
        return reasons

    # ------------------------------------------------------------------ setups

    def detect_setups(self, snap: TokenSnapshot, now: datetime) -> list[Setup]:
        candles = ta.window(snap.candles_5m, now, 6 * 60)
        if len(candles) < 18:
            return []
        price = float(snap.price_usd)
        out = []
        for fn in (self._pullback, self._breakout_retest, self._range_base):
            s = fn(candles, price, snap, now)
            if s:
                out.append(s)
        return out

    def _pullback(self, cs, price, snap, now) -> Setup | None:
        c = self.c
        peak_i = max(range(len(cs)), key=lambda i: cs[i].high)
        if peak_i < 6 or peak_i > len(cs) - 3:
            return None
        start_i = min(range(peak_i), key=lambda i: cs[i].low)
        start, peak = cs[start_i].low, cs[peak_i].high
        move = peak - start
        if move <= 0 or ta.pct_change(start, peak) < 50:
            return None
        retrace = (peak - price) / move * 100
        if not (c.pullback_min_retrace_pct <= retrace <= c.pullback_max_retrace_pct):
            return None
        # earlier consolidation zone that contains the current price
        best = None
        for i in range(start_i, max(start_i, peak_i - 6) + 1):
            block = cs[i : i + 6]
            if len(block) < 6:
                break
            lo = min(x.low for x in block)
            hi = max(x.high for x in block)
            if lo <= price <= hi * 1.02 and ta.pct_change(lo, hi) <= 20:
                if best is None or (hi - lo) < (best[1] - best[0]):
                    best = (lo, hi)
        if best is None:
            return None
        up_vol = sum(x.volume_usd for x in cs[start_i : peak_i + 1]) / (peak_i - start_i + 1)
        since = cs[peak_i + 1 :]
        down_vol = sum(x.volume_usd for x in since) / max(len(since), 1)
        if up_vol <= 0 or down_vol / up_vol > c.pullback_volume_dryup_ratio:
            return None
        stop = best[0] * (1 - c.stop_buffer_pct / 100)
        return Setup("pullback_to_support", best, stop, peak,
                     f"pulled back {retrace:.0f}% of a {ta.pct_change(start, peak):.0f}% move into a prior zone; volume {down_vol / up_vol:.0%} of up-move")

    def _breakout_retest(self, cs, price, snap, now) -> Setup | None:
        c = self.c
        n = 12
        for j in range(len(cs) - 3, n - 1, -1):
            rng = cs[j - n : j]
            hi = max(x.high for x in rng)
            lo = min(x.low for x in rng)
            if ta.pct_change(lo, hi) > c.range_max_width_pct:
                continue
            avg_vol = sum(x.volume_usd for x in rng) / n
            bo = cs[j]
            if not (bo.close > hi and bo.volume_usd >= c.breakout_volume_mult * avg_vol):
                continue
            post = cs[j + 1 :]
            came_back = min(x.low for x in post) <= hi * (1 + c.retest_tolerance_pct / 100)
            holding = post[-1].close >= hi and price >= hi * (1 - c.retest_tolerance_pct / 100)
            if not (came_back and holding):
                return None
            bo_high = max(x.high for x in cs[j:])
            zone = (hi, hi * (1 + c.retest_tolerance_pct / 100))
            stop = (hi + lo) / 2  # back inside the range
            return Setup("breakout_retest", zone, stop, bo_high,
                         f"broke a {ta.pct_change(lo, hi):.0f}% range on {bo.volume_usd / avg_vol:.1f}x volume and retested it")
        return None

    def _range_base(self, cs, price, snap, now) -> Setup | None:
        c = self.c
        n = int(c.range_min_hours * 12)
        if len(cs) < n + 6:
            return None
        rng = cs[-n - 1 : -1]
        hi = max(x.high for x in rng)
        lo = min(x.low for x in rng)
        if ta.pct_change(lo, hi) > c.range_max_width_pct:
            return None
        before = cs[: -n - 1]
        pump_high = max(x.high for x in before) if before else hi
        if pump_high <= hi * 1.05:
            return None  # needs a first pump before the base
        holders = ta.series_trend(snap.holder_history, now, c.range_min_hours * 60)
        if holders is None or holders <= 0:
            return None
        avg_vol = sum(x.volume_usd for x in rng) / n
        last = cs[-1]
        if last.volume_usd < c.volume_expansion_mult * avg_vol:
            return None
        zone = ((hi + lo) / 2, hi * (1 + c.retest_tolerance_pct / 100))
        stop = lo * (1 - c.stop_buffer_pct / 100)
        return Setup("range_base", zone, stop, pump_high,
                     f"{c.range_min_hours}h base ({ta.pct_change(lo, hi):.0f}% wide), holders +{holders:.0f}%, volume {last.volume_usd / avg_vol:.1f}x")

    # ------------------------------------------------------------------ helpers

    def _no_entry(self, record: Record, reasons: list[str]) -> EntryBlock:
        block = EntryBlock(
            decision="NO_ENTRY",
            setup=None,
            entry_zone=None,
            size_usd=0.0,
            tranches=[],
            stop_price=None,
            invalidation=None,
            first_target=None,
            reward_to_risk=None,
            max_slippage_pct=min(self.c.default_slippage_pct, self.c.max_slippage_pct),
            expires_at=None,
            reasons=reasons,
        )
        record.entry = block
        return block


def _r(x: float) -> float:
    return float(f"{x:.6g}")
