"""Bot 4 — Exit (when to get out and why).

Watches every open position and outputs HOLD, TRIM or EXIT. Every exit names
its trigger and evidence. Feelings ("it'll come back") never beat a trigger.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from . import ta
from .config import Section
from .models import ExitBlock, GatekeeperBlock, SafetyReport, TokenSnapshot, iso, is_unknown, known
from .portfolio import Position


@dataclass
class ExitDecision:
    block: ExitBlock
    tokens_to_sell: float
    emergency: bool = False
    new_stop: float | None = None
    mark_flags: tuple[str, ...] = ()


class Exit:
    def __init__(self, cfg: Section):
        self.cfg = cfg
        self.c = cfg.exit

    def evaluate(
        self,
        pos: Position,
        snap: TokenSnapshot,
        safety: SafetyReport | None,
        recheck: GatekeeperBlock | None,
        now: datetime,
    ) -> ExitDecision:
        if is_unknown(snap.price_usd):
            return self._hold(pos, "price UNKNOWN — cannot evaluate; retrying next tick (exits keep running)")
        price = float(snap.price_usd)

        closed = [c for c in snap.candles_5m if c.ts >= pos.opened_at]
        if closed:
            pos.highest_close = max(pos.highest_close, max(c.close for c in closed))
        pos.highest_close = max(pos.highest_close, pos.avg_entry)

        for check in (self._emergency, self._invalidation, self._ladder, self._trailing, self._decay, self._time_stop):
            decision = check(pos, snap, safety, recheck, price, now)
            if decision:
                return decision
        return self._hold(pos, f"no trigger; price {price:.6g}, stop {pos.stop_price:.6g}, target {pos.first_target:.6g}")

    # ------------------------------------------------------------------ 1. emergency

    def _emergency(self, pos, snap, safety, recheck, price, now) -> ExitDecision | None:
        hits: list[str] = []
        if recheck is not None and recheck.verdict == "REJECT":
            hits.append("Gatekeeper re-check returned REJECT: " + "; ".join(recheck.hard_fails[:2] or recheck.top_risks[:2]))

        w = self.c.emergency_liq_window_min
        recent = [v for t, v in snap.liquidity_history if t >= now - timedelta(minutes=w)]
        if recent and known(snap.liquidity_usd):
            peak = max(recent + [snap.liquidity_usd])
            drop = (peak - snap.liquidity_usd) / peak * 100 if peak else 0
            if drop >= self.c.emergency_liq_drop_pct:
                hits.append(f"liquidity -{drop:.0f}% in {w} min (${peak:,.0f} -> ${snap.liquidity_usd:,.0f})")

        if safety is not None:
            if safety.sell_simulation_ok is False:
                hits.append("sell simulation fails")
            if safety.mint_authority_active is True:
                hits.append("mint authority returned")
            if safety.freeze_authority_active is True:
                hits.append("freeze authority returned")
            if safety.tax_mutable is True or (
                known(safety.sell_tax_pct, pos.entry_sell_tax_pct) and safety.sell_tax_pct > pos.entry_sell_tax_pct + 0.01
            ):
                hits.append(f"taxes changed (sell tax now {safety.sell_tax_pct})")
            tk = self.cfg.gatekeeper.thesis_killers
            if known(safety.dev_holding_pct, pos.dev_holding_at_entry) and pos.dev_holding_at_entry > 0:
                sold = (pos.dev_holding_at_entry - safety.dev_holding_pct) / pos.dev_holding_at_entry * 100
                if sold > tk.dev_sell_pct:
                    hits.append(f"thesis killer: dev wallet sold {sold:.0f}% of its tokens")
            if safety.insiders_selling is True and pos.entry_insiders_selling is False:
                hits.append("thesis killer: insiders started selling into strength")

        if not hits:
            return None
        return ExitDecision(
            ExitBlock(
                action="EXIT",
                pct_of_position=100,
                trigger="emergency",
                priority=1,
                evidence=f"{'; '.join(hits)} at {iso(now)}",
                next="Emergency exit in one fast transaction (up to 15% slippage); journal it; 2h re-entry cooldown",
            ),
            tokens_to_sell=pos.tokens,
            emergency=True,
        )

    # ------------------------------------------------------------------ 2. invalidation

    def _invalidation(self, pos, snap, safety, recheck, price, now) -> ExitDecision | None:
        closed = [c for c in snap.candles_5m if c.ts >= pos.opened_at]
        if not closed:
            return None
        last = closed[-1]
        if last.close >= pos.stop_price:
            return None
        trigger = "invalidation" if pos.stop_price <= pos.initial_stop else "stop"
        return ExitDecision(
            ExitBlock(
                action="EXIT",
                pct_of_position=100,
                trigger=trigger,
                priority=2,
                evidence=f"5-min candle closed {last.close:.6g} below stop {pos.stop_price:.6g} at {iso(last.ts + timedelta(minutes=5))}",
                next="Exit in chunks (<=1% impact each); journal; 2h cooldown",
            ),
            tokens_to_sell=pos.tokens,
        )

    # ------------------------------------------------------------------ 3. profit ladder

    def _ladder(self, pos, snap, safety, recheck, price, now) -> ExitDecision | None:
        c = self.c
        original = pos.tokens_bought
        runner_floor = original * 0.20

        def capped(tokens: float) -> float:
            return max(0.0, min(tokens, pos.tokens - runner_floor))

        if not pos.first_target_done and price >= pos.first_target:
            sell = capped(original * c.ladder_first_target_sell_pct / 100)
            return ExitDecision(
                ExitBlock("TRIM", _pct(sell, pos.tokens), "profit_ladder_first_target", 3,
                          f"Price {price:.6g} at {iso(now)}, above first target {pos.first_target:.6g}",
                          "Stop moved to break-even"),
                tokens_to_sell=sell,
                new_stop=pos.avg_entry,
                mark_flags=("first_target_done",),
            )
        if not pos.double_done and price >= pos.avg_entry * c.ladder_double_multiple:
            need = max(0.0, pos.cost_usd - pos.proceeds_usd) * 1.02  # buffer for fees/impact
            sell = capped(need / price)
            return ExitDecision(
                ExitBlock("TRIM", _pct(sell, pos.tokens), "profit_ladder_2x", 3,
                          f"Price {price:.6g} = {price / pos.avg_entry:.2f}x avg entry at {iso(now)}",
                          "Total sales now recover full cost; remainder is house money"),
                tokens_to_sell=sell,
                new_stop=max(pos.stop_price, pos.avg_entry),
                mark_flags=("double_done", "first_target_done"),
            )
        if not pos.triple_done and price >= pos.avg_entry * c.ladder_triple_multiple:
            sell = capped(original * c.ladder_triple_sell_pct_of_original / 100)
            return ExitDecision(
                ExitBlock("TRIM", _pct(sell, pos.tokens), "profit_ladder_3x", 3,
                          f"Price {price:.6g} = {price / pos.avg_entry:.2f}x avg entry at {iso(now)}",
                          "Remaining 20-25% rides as a runner under the trailing stop"),
                tokens_to_sell=sell,
                mark_flags=("triple_done", "double_done", "first_target_done"),
            )
        return None

    # ------------------------------------------------------------------ 4. trailing stop

    def trail_level(self, pos: Position, snap: TokenSnapshot) -> float:
        from_high = pos.highest_close * (1 - self.c.trail_from_high_close_pct / 100)
        hl = ta.last_higher_low([c for c in snap.candles_15m if c.ts >= pos.opened_at])
        return max(from_high, hl or 0.0)  # whichever is tighter

    def _trailing(self, pos, snap, safety, recheck, price, now) -> ExitDecision | None:
        active = pos.triple_done if self.c.trail_activates_after == "triple" else pos.first_target_done
        if not active:
            return None
        level = self.trail_level(pos, snap)
        if price >= level:
            return None
        return ExitDecision(
            ExitBlock("EXIT", 100, "trailing_stop", 4,
                      f"Price {price:.6g} broke trailing level {level:.6g} (highest close {pos.highest_close:.6g}) at {iso(now)}",
                      "Runner closed; journal"),
            tokens_to_sell=pos.tokens,
        )

    # ------------------------------------------------------------------ 5. momentum decay

    def decay_signals(self, pos: Position, snap: TokenSnapshot, now: datetime) -> list[str]:
        c = self.c
        sig = []
        hours = [h for h in snap.candles_1h if h.ts >= pos.opened_at - timedelta(hours=1)][-6:]
        if len(hours) >= 2:
            peak = max(h.volume_usd for h in hours)
            last = hours[-1]
            stall = abs(ta.pct_change(last.open, last.close)) < 5
            if peak > 0 and last.volume_usd <= peak * (1 - c.decay_volume_drop_pct / 100) and stall:
                sig.append(f"hourly volume {last.volume_usd / peak:.0%} of peak while price stalls")
        if ta.lower_high_lower_low([c for c in snap.candles_15m if c.ts >= pos.opened_at]):
            sig.append("lower high and lower low on 15m")
        window = [v for t, v in snap.buy_sell_history if t >= now - timedelta(minutes=c.decay_ratio_window_min)]
        covered = [t for t, _ in snap.buy_sell_history if t >= now - timedelta(minutes=c.decay_ratio_window_min)]
        if len(window) >= 3 and (covered[-1] - covered[0]) >= timedelta(minutes=c.decay_ratio_window_min * 0.8) and all(v < c.decay_buy_sell_ratio for v in window):
            sig.append(f"buy/sell ratio < {c.decay_buy_sell_ratio} for {c.decay_ratio_window_min} min")
        else:
            h = ta.series_trend(snap.holder_history, now, c.decay_holder_fall_window_min)
            if h is not None and h < 0:
                sig.append(f"holder count down {h:.1f}% over {c.decay_holder_fall_window_min} min")
        if snap.proven_wallets_selling is True:
            sig.append("proven wallets that bought earlier are selling")
        if known(snap.social_mentions_1h, snap.social_mentions_peak_1h) and snap.social_mentions_peak_1h > 0:
            if snap.social_mentions_1h <= snap.social_mentions_peak_1h * (1 - c.decay_social_drop_pct / 100):
                sig.append("social mentions down 50%+ from peak")
        return sig

    def _decay(self, pos, snap, safety, recheck, price, now) -> ExitDecision | None:
        sig = self.decay_signals(pos, snap, now)
        if len(sig) >= 2:
            return ExitDecision(
                ExitBlock("EXIT", 100, "momentum_decay", 5, f"{len(sig)} decay signals: {'; '.join(sig)} at {iso(now)}", "Exit in chunks; journal"),
                tokens_to_sell=pos.tokens,
            )
        if len(sig) == 1 and not pos.decay_trimmed:
            closed15 = [c for c in snap.candles_15m if c.ts >= pos.opened_at]
            tighter = min((c.low for c in closed15[-2:]), default=pos.stop_price)
            new_stop = max(pos.stop_price, min(tighter, price * 0.99))
            sell = pos.tokens * self.c.decay_trim_pct / 100
            return ExitDecision(
                ExitBlock("TRIM", self.c.decay_trim_pct, "momentum_decay", 5, f"1 decay signal: {sig[0]} at {iso(now)}",
                          f"Stop tightened to {new_stop:.6g}"),
                tokens_to_sell=sell,
                new_stop=new_stop,
                mark_flags=("decay_trimmed",),
            )
        return None

    # ------------------------------------------------------------------ 6. time stop

    def _time_stop(self, pos, snap, safety, recheck, price, now) -> ExitDecision | None:
        if pos.first_target_done or now - pos.opened_at < timedelta(hours=self.c.time_stop_hours):
            return None
        needed = pos.avg_entry + self.c.time_stop_progress_frac * (pos.first_target - pos.avg_entry)
        if pos.highest_close >= needed:
            return None
        return ExitDecision(
            ExitBlock("EXIT", 100, "time_stop", 6,
                      f"No progress toward first target {pos.first_target:.6g} in {self.c.time_stop_hours}h (best close {pos.highest_close:.6g})",
                      "Dead money still carries rug risk; exit in chunks"),
            tokens_to_sell=pos.tokens,
        )

    # ------------------------------------------------------------------ helpers

    def _hold(self, pos: Position, evidence: str) -> ExitDecision:
        return ExitDecision(ExitBlock("HOLD", 0, None, None, evidence, "Keep monitoring every minute"), tokens_to_sell=0.0)


def _pct(part: float, whole: float) -> float:
    return round(part / whole * 100, 1) if whole else 0.0
