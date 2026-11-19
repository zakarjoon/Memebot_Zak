"""Provider interface. Bots never talk to the network; the orchestrator asks a
provider, and anything a provider cannot get comes back as UNKNOWN."""
from __future__ import annotations

from datetime import datetime
from typing import Protocol

from ..models import MarketContext, Num, SafetyReport, TokenSnapshot


class DataProvider(Protocol):
    name: str

    def now(self) -> datetime: ...

    async def discover(self) -> tuple[list[str], dict[str, str]]:
        """Return (candidate token addresses, {SYMBOL: canonical address} for copycat checks)."""

    async def snapshots(self, addresses: list[str], full: bool = True) -> dict[str, TokenSnapshot]:
        """Light snapshots for many tokens; ``full`` also loads candles and holders."""

    async def safety(self, snap: TokenSnapshot) -> SafetyReport: ...

    async def market_context(self) -> MarketContext: ...

    async def quote_impacts(self, snap: TokenSnapshot, usd: float) -> tuple[Num, Num]:
        """Price impact % for buying ``usd`` and for selling that size right back."""

    async def close(self) -> None: ...
