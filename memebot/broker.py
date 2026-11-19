"""Order execution. Paper mode is the default and the only implemented broker.

Live execution is deliberately not implemented: the spec requires 50+ paper
trades with positive expectancy first, and the human typing ``LIVE MODE ON``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from .config import Section
from .portfolio import Fill

LIVE_PHRASE = "LIVE MODE ON"


class ExecutionError(Exception):
    pass


def pool_impact_pct(usd: float, liquidity_usd: float) -> float:
    """Price impact of a swap of ``usd`` into a constant-product pool whose total
    liquidity (both sides) is ``liquidity_usd``. Half the liquidity is the quote side."""
    side = liquidity_usd / 2
    if side <= 0:
        return 100.0
    return usd / (side + usd) * 100


@dataclass
class PaperBroker:
    cfg: Section
    mode: str = "paper"

    def _fee(self, usd: float) -> float:
        return usd * self.cfg.broker.swap_fee_pct / 100 + self.cfg.broker.network_fee_usd

    def buy(self, now: datetime, usd: float, price: float, liquidity_usd: float, max_slippage_pct: float, reason: str) -> Fill:
        impact = pool_impact_pct(usd, liquidity_usd)
        if impact > max_slippage_pct:
            raise ExecutionError(f"buy impact {impact:.2f}% exceeds slippage limit {max_slippage_pct}%")
        fee = self._fee(usd)
        eff_price = price * (1 + impact / 100)
        tokens = (usd - fee) / eff_price
        return Fill(now, "buy", tokens, eff_price, usd, fee, impact, max_slippage_pct, reason)

    def sell(
        self,
        now: datetime,
        tokens: float,
        price: float,
        liquidity_usd: float,
        *,
        emergency: bool,
        reason: str,
    ) -> list[Fill]:
        """Normal exits are chunked so no single sell moves price more than 1%.
        Emergency exits go in one fast transaction accepting up to 15% slippage,
        falling back to rapid chunks if the pool cannot absorb it."""
        e = self.cfg.exit
        gross = tokens * price
        whole_impact = pool_impact_pct(gross, liquidity_usd)
        if emergency and whole_impact <= e.emergency_max_slippage_pct:
            chunks = 1
            limit = e.emergency_max_slippage_pct
        else:
            per_chunk = e.emergency_max_slippage_pct if emergency else e.normal_chunk_max_impact_pct
            chunks = max(1, math.ceil(whole_impact / per_chunk))
            limit = per_chunk
        fills = []
        chunk_tokens = tokens / chunks
        for _ in range(chunks):
            usd_gross = chunk_tokens * price
            impact = min(pool_impact_pct(usd_gross, liquidity_usd), 99.0)
            eff = price * (1 - impact / 100)
            proceeds = chunk_tokens * eff
            fee = self._fee(proceeds)
            fills.append(Fill(now, "sell", chunk_tokens, eff, max(proceeds - fee, 0.0), fee, impact, limit, reason))
        return fills


class LiveBroker:  # pragma: no cover - intentionally unimplemented
    def __init__(self, *_, **__):
        raise ExecutionError(
            "Live trading is not implemented in this build. Paper-trade until the weekly review shows "
            "50+ trades with positive expectancy after fees and slippage, then wire a reviewed live "
            "executor (Jupiter swap + MEV-protected route, dedicated hot wallet holding only trading capital)."
        )


def make_broker(cfg: Section, live_confirmation: str | None = None):
    if cfg.mode == "live":
        if live_confirmation != LIVE_PHRASE:
            raise ExecutionError(f"mode is 'live' but the human has not typed '{LIVE_PHRASE}'. Staying out.")
        return LiveBroker(cfg)
    return PaperBroker(cfg)
