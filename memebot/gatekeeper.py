"""Bot 2 — Gatekeeper (risk analysis).

Default answer is REJECT. A token earns PASS only when the evidence clears every
check. PASS means "no red flags found", never "safe".
"""
from __future__ import annotations

from datetime import datetime

from . import ta
from .config import Section
from .models import (
    UNKNOWN,
    GatekeeperBlock,
    MarketContext,
    Record,
    SafetyReport,
    TokenSnapshot,
    iso,
    is_unknown,
    known,
)


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


class Gatekeeper:
    def __init__(self, cfg: Section):
        self.cfg = cfg
        self.c = cfg.gatekeeper

    # ------------------------------------------------------------------ public

    def check(
        self,
        record: Record,
        snap: TokenSnapshot,
        safety: SafetyReport,
        market: MarketContext,
        now: datetime,
    ) -> GatekeeperBlock:
        hard_fails, unknown_critical = self.hard_fails(snap, safety)
        categories, sub_unknowns = self.risk_categories(snap, safety, market, now)
        risk_score = int(round(sum(categories.values())))
        unknowns = unknown_critical + sub_unknowns
        confidence = "high" if not unknowns else ("medium" if len(unknowns) <= 3 else "low")

        if unknown_critical:
            hard_fails.append("Unknown critical data after retries: " + ", ".join(unknown_critical))

        if hard_fails:
            block = GatekeeperBlock(
                verdict="REJECT",
                hard_fails=hard_fails,
                risk_score=risk_score,
                tier=None,
                size_multiplier=0.0,
                top_risks=hard_fails[:3],
                thesis_killers=[],
                unknowns=unknowns,
                checked_at=iso(now),
                category_scores={k: round(v, 1) for k, v in categories.items()},
                confidence=confidence,
            )
        else:
            tier = self.tier_for(risk_score)
            block = GatekeeperBlock(
                verdict=tier["verdict"],
                hard_fails=[],
                risk_score=risk_score,
                tier=tier["tier"],
                size_multiplier=float(tier["size_multiplier"]),
                top_risks=self._top_risks(categories, snap, safety),
                thesis_killers=self.thesis_killers() if tier["verdict"] != "REJECT" else [],
                unknowns=unknowns,
                checked_at=iso(now),
                category_scores={k: round(v, 1) for k, v in categories.items()},
                confidence=confidence,
            )
        record.gatekeeper = block
        return block

    def tier_for(self, risk_score: float) -> dict:
        for tier in self.c.tiers:
            if risk_score <= tier["max_score"]:
                return tier
        return self.c.tiers[-1]

    def thesis_killers(self) -> list[str]:
        tk = self.c.thesis_killers
        return [
            f"Dev wallet sells >{tk.dev_sell_pct}% of its tokens",
            f"Liquidity -{tk.liquidity_drop_pct}% in {tk.liquidity_drop_window_min} min",
            "Top holder sends to an exchange",
            "Mint or freeze authority returns, or taxes change",
        ]

    # ------------------------------------------------------------------ stage 1

    def hard_fails(self, snap: TokenSnapshot, s: SafetyReport) -> tuple[list[str], list[str]]:
        c = self.c
        fails: list[str] = []
        unknown: list[str] = []

        def flag(name: str, value, message: str) -> None:
            if is_unknown(value):
                unknown.append(name)
            elif value:
                fails.append(message)

        flag("sell_simulation", None if is_unknown(s.sell_simulation_ok) else (not s.sell_simulation_ok),
             "Honeypot: simulated sell fails")

        if is_unknown(s.sell_tax_pct):
            unknown.append("sell_tax")
        elif s.sell_tax_pct > c.max_sell_tax_pct:
            fails.append(f"Taxes: sell tax {s.sell_tax_pct:.1f}% > {c.max_sell_tax_pct}%")
        flag("tax_mutable", s.tax_mutable, "Taxes: owner can still change the tax")
        flag("mint_authority", s.mint_authority_active, "Mint: mint authority not revoked")
        flag("freeze_authority", s.freeze_authority_active, "Freeze: freeze authority active")

        if is_unknown(s.lp_locked_pct):
            unknown.append("lp_lock")
        elif s.lp_locked_pct < c.min_lp_locked_pct:
            fails.append(f"Liquidity: only {s.lp_locked_pct:.0f}% of LP burned/locked")
        elif is_unknown(s.lp_lock_days_remaining):
            unknown.append("lp_lock_duration")
        elif s.lp_lock_days_remaining < c.min_lp_lock_days:
            fails.append(f"Liquidity: LP lock ends in {s.lp_lock_days_remaining:.0f} days")

        flag("upgradeable", s.upgradeable, "Upgradeable: deployer controls an upgrade path / transfer hook")

        biggest = self.biggest_holder_pct(s)
        if biggest is None:
            unknown.append("holder_list")
        elif biggest > c.max_single_wallet_pct:
            fails.append(f"Whale wallet: one holder (or funded cluster) owns {biggest:.1f}% of supply")

        flag("deployer_history", s.deployer_rugged_before, "Deployer history: deployer or its funded wallets rugged before")
        flag("impersonation", s.impersonation, "Impersonation: name/ticker copies another project")
        return fails, unknown

    @staticmethod
    def _effective_holders(s: SafetyReport) -> list[float]:
        """Holder shares excluding LP/burn/programs, with funded clusters merged into one."""
        live = {h.address: h.pct for h in s.top_holders if not h.excluded}
        merged: list[float] = []
        seen: set[str] = set()
        for cluster in s.holder_clusters:
            members = [a for a in cluster if a in live]
            if members:
                merged.append(sum(live[a] for a in members))
                seen.update(members)
        merged += [pct for addr, pct in live.items() if addr not in seen]
        return sorted(merged, reverse=True)

    def biggest_holder_pct(self, s: SafetyReport) -> float | None:
        if not s.top_holders:
            return None
        holders = self._effective_holders(s)
        return holders[0] if holders else 0.0

    def top10_pct(self, s: SafetyReport) -> float | None:
        if not s.top_holders:
            return None
        return sum(self._effective_holders(s)[:10])

    # ------------------------------------------------------------------ stage 2

    def risk_categories(
        self, snap: TokenSnapshot, s: SafetyReport, m: MarketContext, now: datetime
    ) -> tuple[dict[str, float], list[str]]:
        """Each category = weight x mean(sub-risks). Every UNKNOWN sub-risk counts as 1.0."""
        c, w = self.c, self.c.weights
        unknowns: list[str] = []

        def sub(name: str, value):
            if value is None or is_unknown(value):
                unknowns.append(name)
                return 1.0
            return _clamp(float(value))

        cats: dict[str, list[float]] = {}

        top10 = self.top10_pct(s)
        cats["holder_concentration"] = [
            sub("top10_share", None if top10 is None else (top10 - 10) / max(c.top10_high_risk_pct - 10, 1))
        ]

        cats["insider_sniper"] = [
            sub("insider_supply", None if is_unknown(s.insider_supply_pct) else s.insider_supply_pct / c.insider_high_risk_pct),
            sub("dev_holding", None if is_unknown(s.dev_holding_pct) else s.dev_holding_pct / 10),
            sub("insiders_selling", None if is_unknown(s.insiders_selling) else float(s.insiders_selling)),
        ]

        ratio = None
        if known(snap.liquidity_usd, snap.mcap_usd) and snap.mcap_usd > 0:
            ratio = snap.liquidity_usd / snap.mcap_usd * 100
        liq_trend = ta.series_trend(snap.liquidity_history, now, 60)
        lock_days = s.lp_lock_days_remaining
        cats["liquidity_health"] = [
            sub("liq_to_mcap", None if ratio is None else 1 - ratio / c.min_liq_to_mcap_pct),
            sub("lp_lock_length", None if is_unknown(lock_days) else (0.0 if lock_days >= 365 else 1 - lock_days / 365)),
            sub("liquidity_trend", None if liq_trend is None else -liq_trend / 20),
        ]

        trader_risk = None
        if known(snap.unique_traders_1h, snap.buys_1h, snap.sells_1h) and (snap.buys_1h + snap.sells_1h) > 0:
            tr = snap.unique_traders_1h / (snap.buys_1h + snap.sells_1h)
            low = c.unique_trader_ratio_low
            trader_risk = 1 - (tr - low) / low  # 1.0 at `low`, 0 at 2x low
        spike_risk = None
        c5 = ta.window(snap.candles_5m, now, 120)
        if len(c5) >= 6:
            vols = sorted(x.volume_usd for x in c5)
            median = vols[len(vols) // 2] or 1
            flat_spikes = [x for x in c5 if x.volume_usd > 3 * median and abs(ta.pct_change(x.open, x.close)) < 2]
            spike_risk = len(flat_spikes) / len(c5) * 4
        cats["volume_authenticity"] = [sub("unique_traders", trader_risk), sub("volume_spikes", spike_risk)]

        cats["deployer_history"] = [
            sub("deployer_age", None if is_unknown(s.deployer_age_days) else float(s.deployer_age_days < c.min_deployer_age_days)),
            sub("deployer_funds", None if is_unknown(s.deployer_moving_funds) else float(s.deployer_moving_funds)),
        ]

        cats["social_authenticity"] = [
            sub("bot_followers", None if is_unknown(s.bot_followers) else float(s.bot_followers)),
            sub("copied_website", None if is_unknown(s.copied_website) else float(s.copied_website)),
            sub("paid_promoters", None if is_unknown(snap.social_paid_or_new_only) else float(snap.social_paid_or_new_only)),
        ]

        cats["market_context"] = [
            sub("base_coin_trend", None if is_unknown(m.base_coin_trend_24h_pct) else (2 - m.base_coin_trend_24h_pct) / 7),
            sub("sector_volume", None if is_unknown(m.meme_sector_volume_trend_pct) else -m.meme_sector_volume_trend_pct / 30),
        ]

        scores = {name: w[name] * (sum(parts) / len(parts)) for name, parts in cats.items()}
        return scores, unknowns

    def _top_risks(self, cats: dict[str, float], snap: TokenSnapshot, s: SafetyReport) -> list[str]:
        w = self.c.weights
        ranked = sorted(cats.items(), key=lambda kv: kv[1] / w[kv[0]], reverse=True)
        out = []
        for name, value in ranked[:3]:
            share = value / w[name]
            detail = {
                "holder_concentration": f"Top 10 hold {self.top10_pct(s) or 0:.0f}%" if s.top_holders else "Holder list UNKNOWN",
                "insider_sniper": f"Dev wallet holds {s.dev_holding_pct}%" if not is_unknown(s.dev_holding_pct) else "Insider/dev supply UNKNOWN",
                "liquidity_health": "Liquidity thin or not deepening",
                "volume_authenticity": "Volume may be inorganic",
                "deployer_history": "Deployer young or history UNKNOWN",
                "social_authenticity": "Social authenticity UNKNOWN or weak",
                "market_context": "Weak base-chain / sector backdrop",
            }[name]
            out.append(f"{detail} ({share:.0%} of {name} risk)")
        return out


__all__ = ["Gatekeeper", "UNKNOWN"]
