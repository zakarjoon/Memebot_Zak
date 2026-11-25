"""Solana data from free, no-key public endpoints.

Sources (confirm each is still live and accurate before relying on it):
  * DEX Screener  — discovery, price, liquidity, volume, txns      (api.dexscreener.com)
  * GeckoTerminal — trending/new pools, 5m OHLCV, holder counts    (api.geckoterminal.com, ~30 req/min)
  * RugCheck      — mint/freeze authority, LP lock, holders, risks (api.rugcheck.xyz)
  * Jupiter       — buy/sell quotes for price impact & sell checks (lite-api.jup.ag)

What free sources do NOT give you (left UNKNOWN, which the bots treat as risk):
proven-wallet activity, social velocity/authenticity, deployer wallet age and
fund flows, unique trader counts. Add paid sources (Birdeye, Helius, GMGN,
Bubblemaps, an X API) by filling those fields in ``snapshots``/``safety``.

A Jupiter quote is a *proxy* for a sell simulation: it proves a route exists,
not that a signed transaction would succeed. Freeze authority and Token-2022
transfer hooks — the main Solana honeypot mechanisms — are checked separately.
"""
from __future__ import annotations

import asyncio
import time
import logging
from datetime import datetime, timedelta, timezone

from .. import ta
from ..config import Section
from ..models import UNKNOWN, Candle, Holder, MarketContext, SafetyReport, TokenSnapshot, is_unknown, utcnow
from .http import Http

log = logging.getLogger("memebot.solana")

DEXS = "https://api.dexscreener.com"
GT = "https://api.geckoterminal.com/api/v2"
RUGCHECK = "https://api.rugcheck.xyz/v1"
JUP = "https://lite-api.jup.ag/swap/v1"
GT_HEADERS = {"Accept": "application/json;version=20230302"}

SOL = "So11111111111111111111111111111111111111112"
BURN_ADDRESSES = {"1nc1nerator11111111111111111111111111111111", "11111111111111111111111111111111"}
NO_ROUTE_CODES = ("COULD_NOT_FIND_ANY_ROUTE", "TOKEN_NOT_TRADABLE", "NO_ROUTES_FOUND")


def _f(x) -> float | str:
    try:
        return float(x)
    except (TypeError, ValueError):
        return UNKNOWN


def _num(x) -> float:
    return 0.0 if is_unknown(x) else float(x)


class SolanaFreeProvider:
    name = "solana_free"

    def __init__(self, cfg: Section, transport=None):
        d = cfg.data
        self.cfg = cfg
        self.http = Http(d.timeout_sec, d.retries, d.backoff_base_sec, transport=transport,
                         rate_limit_wait=d.get("rate_limit_wait_sec", 20.0),
                         limits=dict(d.rate_limits_per_min))
        self._decimals: dict[str, int] = {}
        self._gt_info: dict[str, dict] = {}  # GeckoTerminal token info, a second opinion on safety
        self._rc_cache: dict[str, tuple[float, object]] = {}  # RugCheck reports, reused for ~1 min
        self._sol_usd: float | str = UNKNOWN

    def now(self) -> datetime:
        return utcnow()

    async def close(self) -> None:
        await self.http.close()

    # ------------------------------------------------------------------ discovery

    async def discover(self) -> tuple[list[str], dict[str, str]]:
        limit = self.cfg.data.discovery_limit
        profiles, boosts, trending, new = await asyncio.gather(
            self.http.get_json(f"{DEXS}/token-profiles/latest/v1"),
            self.http.get_json(f"{DEXS}/token-boosts/top/v1"),
            self.http.get_json(f"{GT}/networks/solana/trending_pools", headers=GT_HEADERS),
            self.http.get_json(f"{GT}/networks/solana/new_pools", headers=GT_HEADERS),
        )
        addrs: list[str] = []
        self._trending: set[str] = set()

        def add(a: str | None, is_trending: bool = False):
            if a and a != SOL and a not in addrs:
                addrs.append(a)
            if a and is_trending:
                self._trending.add(a)

        for pool_list, is_trending in ((trending, True), (new, False)):
            if not is_unknown(pool_list):
                for p in pool_list.get("data", []):
                    base = p.get("relationships", {}).get("base_token", {}).get("data", {}).get("id", "")
                    add(base.removeprefix("solana_"), is_trending)
        for lst in (profiles, boosts):
            if not is_unknown(lst):
                for item in lst:
                    if item.get("chainId") == "solana":
                        add(item.get("tokenAddress"))
        # Boosted tokens are paid placement: a lead to check, never proof.
        known_symbols: dict[str, str] = {}
        return addrs[:limit], known_symbols

    # ------------------------------------------------------------------ snapshots

    async def snapshots(self, addresses: list[str], full: bool = True) -> dict[str, TokenSnapshot]:
        out: dict[str, TokenSnapshot] = {}
        now = self.now()
        for i in range(0, len(addresses), 30):
            chunk = addresses[i : i + 30]
            pairs = await self.http.get_json(f"{DEXS}/tokens/v1/solana/{','.join(chunk)}")
            if is_unknown(pairs):
                continue
            best: dict[str, dict] = {}
            for p in pairs:
                a = p.get("baseToken", {}).get("address")
                if a not in chunk:
                    continue
                liq = p.get("liquidity", {}).get("usd") or 0
                if a not in best or liq > (best[a].get("liquidity", {}).get("usd") or 0):
                    best[a] = p
            for a, p in best.items():
                out[a] = self._from_pair(a, p, now)
        if full:
            await asyncio.gather(*(self._enrich(s) for s in out.values()))
        return out

    def _from_pair(self, address: str, p: dict, now: datetime) -> TokenSnapshot:
        txh1 = p.get("txns", {}).get("h1", {})
        created = p.get("pairCreatedAt")
        age = (now - datetime.fromtimestamp(created / 1000, tz=timezone.utc)).total_seconds() / 60 if created else UNKNOWN
        sources = ["dexscreener"]
        if address in getattr(self, "_trending", set()):
            sources.append("geckoterminal_trending")
        return TokenSnapshot(
            chain="solana",
            address=address,
            symbol=p.get("baseToken", {}).get("symbol", "?"),
            name=p.get("baseToken", {}).get("name", "?"),
            pair_address=p.get("pairAddress", UNKNOWN),
            data_timestamp=now,
            price_usd=_f(p.get("priceUsd")),
            liquidity_usd=_f(p.get("liquidity", {}).get("usd")),
            mcap_usd=_f(p.get("marketCap") or p.get("fdv")),
            vol_1h_usd=_f(p.get("volume", {}).get("h1")),
            vol_24h_usd=_f(p.get("volume", {}).get("h24")),
            age_min=age,
            price_change_24h_pct=_f(p.get("priceChange", {}).get("h24")),
            buys_1h=txh1.get("buys", UNKNOWN),
            sells_1h=txh1.get("sells", UNKNOWN),
            sources=sources,
        )

    async def _enrich(self, s: TokenSnapshot) -> None:
        now = self.now()
        if not is_unknown(s.pair_address):
            ohlcv = await self.http.get_json(
                f"{GT}/networks/solana/pools/{s.pair_address}/ohlcv/minute",
                params={"aggregate": 5, "limit": 100, "currency": "usd"},
                headers=GT_HEADERS,
            )
            if not is_unknown(ohlcv):
                rows = ohlcv.get("data", {}).get("attributes", {}).get("ohlcv_list", [])
                candles = sorted(
                    (Candle(datetime.fromtimestamp(r[0], tz=timezone.utc), *map(float, r[1:6])) for r in rows),
                    key=lambda c: c.ts,
                )
                s.candles_5m = [c for c in candles if c.ts + timedelta(minutes=5) <= now]  # closed only
                s.candles_15m = ta.aggregate(s.candles_5m, 15, now)
                s.candles_1h = ta.aggregate(s.candles_5m, 60, now)
                s.sources.append("geckoterminal_ohlcv")
        info = await self.http.get_json(f"{GT}/networks/solana/tokens/{s.address}/info", headers=GT_HEADERS)
        if not is_unknown(info):
            self._gt_info[s.address] = info.get("data", {}).get("attributes", {}) or {}
            holders = info.get("data", {}).get("attributes", {}).get("holders") or {}
            count = holders.get("count")
            if count is not None:
                s.holders = float(count)
                s.sources.append("geckoterminal_holders")
        if is_unknown(s.holders):
            report = await self._rugcheck(s.address)
            if not is_unknown(report) and report.get("totalHolders"):
                s.holders = float(report["totalHolders"])
                s.sources.append("rugcheck_holders")

    async def _rugcheck(self, address: str):
        hit = self._rc_cache.get(address)
        if hit and time.monotonic() - hit[0] < 60:
            return hit[1]
        report = await self.http.get_json(f"{RUGCHECK}/tokens/{address}/report")
        if not is_unknown(report):
            self._rc_cache[address] = (time.monotonic(), report)
        return report

    # ------------------------------------------------------------------ safety

    async def safety(self, snap: TokenSnapshot) -> SafetyReport:
        now = self.now()
        rep = SafetyReport(data_timestamp=now)
        report, sell_ok, imp = await asyncio.gather(
            self._rugcheck(snap.address),
            self._sell_route_ok(snap),
            self._impersonation(snap),
        )
        rep.sell_simulation_ok = sell_ok
        rep.impersonation = imp
        if is_unknown(report):
            return rep
        rep.sources.append("rugcheck")
        token = report.get("token") or {}
        if "decimals" in token:
            self._decimals[snap.address] = int(token["decimals"])
        rep.mint_authority_active = bool(report.get("mintAuthority") or token.get("mintAuthority"))
        rep.freeze_authority_active = bool(report.get("freezeAuthority") or token.get("freezeAuthority"))

        risks = report.get("risks") or []
        names = " | ".join(f"{r.get('name', '')} {r.get('description', '')}".lower() for r in risks)
        rep.raw_risks = [r.get("name", "") for r in risks]

        fee = report.get("transferFee") or {}
        rep.sell_tax_pct = float(fee.get("pct") or 0.0)
        authority = fee.get("authority")
        rep.tax_mutable = bool(authority) and authority not in BURN_ADDRESSES
        rep.upgradeable = any(k in names for k in ("permanent delegate", "transfer hook", "upgradeable", "mutable fee"))

        markets = report.get("markets") or []
        with_lp = [m for m in markets if m.get("lp") and m["lp"].get("lpLockedPct") is not None]
        if with_lp:
            # Judge the MAIN pool (deepest liquidity), not the best-looking one: caution wins.
            main = max(with_lp, key=lambda m: (m["lp"].get("baseUSD") or 0) + (m["lp"].get("quoteUSD") or 0))
            rep.lp_locked_pct = float(main["lp"]["lpLockedPct"])
            lockers = report.get("lockers") or {}
            entries = lockers.values() if isinstance(lockers, dict) else lockers
            # unlockDate 0 or in the past means an expired/unrelated locker, not a lock ending now
            future = [l["unlockDate"] for l in entries if (l.get("unlockDate") or 0) > now.timestamp()]
            if rep.lp_locked_pct >= 99:
                rep.lp_lock_days_remaining = float("inf")  # LP burned
            elif future:
                rep.lp_lock_days_remaining = (min(future) - now.timestamp()) / 86400
        pool_addrs = {m.get("pubkey") for m in markets} | {m.get("liquidityA") for m in markets} | {m.get("liquidityB") for m in markets}
        known_accts = report.get("knownAccounts") or {}

        holders = report.get("topHolders") or []
        creator = report.get("creator")
        if holders:
            for h in holders:
                addr = h.get("owner") or h.get("address")
                kind = (known_accts.get(addr) or known_accts.get(h.get("address")) or {}).get("type", "")
                excluded = addr in BURN_ADDRESSES or addr in pool_addrs or h.get("address") in pool_addrs or kind.upper() in ("AMM", "LOCKER", "BURN")
                rep.top_holders.append(Holder(addr, float(h.get("pct") or 0.0), excluded, kind))
            rep.insider_supply_pct = sum(float(h.get("pct") or 0) for h in holders if h.get("insider"))
            if creator:
                rep.deployer_address = creator
                rep.dev_holding_pct = sum(x.pct for x in rep.top_holders if x.address == creator)
        supply = float(token.get("supply") or 0)
        if supply > 0:
            # RugCheck occasionally reports a network holding more than the whole supply; ignore those
            networks = [n for n in (report.get("insiderNetworks") or []) if 0 <= float(n.get("currentHolding") or 0) <= supply]
            held = min(supply, sum(float(n.get("currentHolding") or 0) for n in networks))
            if networks:
                rep.insider_supply_pct = max(_num(rep.insider_supply_pct), held / supply * 100)
            if report.get("creatorBalance") is not None:
                rep.dev_holding_pct = max(_num(rep.dev_holding_pct), float(report["creatorBalance"]) / supply * 100)
                rep.deployer_address = creator or rep.deployer_address
        self._apply_gt_info(rep, snap.address)
        if report.get("totalHolders") and is_unknown(snap.holders):
            snap.holders = float(report["totalHolders"])
        if "rug" in names and "creator" in names:
            rep.deployer_rugged_before = True
        elif report.get("rugged") is True:
            rep.deployer_rugged_before = True
        else:
            rep.deployer_rugged_before = False  # RugCheck checks creator history; absence of the risk = no record found
        return rep

    def _apply_gt_info(self, rep: SafetyReport, address: str) -> None:
        """GeckoTerminal's safety fields as a second opinion. Disagreement resolves to the cautious side."""
        info = self._gt_info.get(address)
        if not info:
            return
        rep.sources.append("geckoterminal_info")
        if info.get("is_honeypot") == "yes":
            rep.sell_simulation_ok = False
        if info.get("mint_authority") == "yes":
            rep.mint_authority_active = True
        if info.get("freeze_authority") == "yes":
            rep.freeze_authority_active = True
        dev = _f(info.get("developer_holding_percentage"))
        if not is_unknown(dev):
            rep.dev_holding_pct = max(_num(rep.dev_holding_pct), dev)

    async def _impersonation(self, snap: TokenSnapshot):
        res = await self.http.get_json(f"{DEXS}/latest/dex/search", params={"q": snap.symbol})
        if is_unknown(res):
            return UNKNOWN
        mine_created, mine_liq = None, 0.0
        others = []
        for p in res.get("pairs") or []:
            bt = p.get("baseToken", {})
            if bt.get("address") == snap.address:
                mine_created = p.get("pairCreatedAt") or mine_created
                mine_liq = max(mine_liq, p.get("liquidity", {}).get("usd") or 0)
            elif (bt.get("symbol", "").upper() == snap.symbol.upper()) and p.get("chainId") in ("solana", "ethereum", "base", "bsc"):
                others.append(p)
        for o in others:
            older = mine_created and o.get("pairCreatedAt") and o["pairCreatedAt"] < mine_created
            # an older project with real, LARGER liquidity under the same ticker — not just any namesake
            liq = o.get("liquidity", {}).get("usd") or 0
            bigger = liq > 250_000 and liq > 2 * mine_liq
            if older and bigger:
                return True
        return False

    # ------------------------------------------------------------------ quotes

    async def _sol_price(self) -> float | str:
        res = await self.http.get_json(f"{DEXS}/tokens/v1/solana/{SOL}")
        if is_unknown(res) or not res:
            return UNKNOWN
        as_base = [p for p in res if p.get("baseToken", {}).get("address") == SOL]  # priceUsd is the BASE token's price
        if not as_base:
            return UNKNOWN
        best = max(as_base, key=lambda p: p.get("liquidity", {}).get("usd") or 0)
        self._sol_usd = _f(best.get("priceUsd"))
        self._sol_change = _f(best.get("priceChange", {}).get("h24"))
        return self._sol_usd

    async def _quote(self, input_mint: str, output_mint: str, amount: int):
        return await self.http.get_json(
            f"{JUP}/quote",
            params={"inputMint": input_mint, "outputMint": output_mint, "amount": amount, "slippageBps": 300, "restrictIntermediateTokens": "true"},
            ok_statuses=(400, 404),
        )

    async def _sell_route_ok(self, snap: TokenSnapshot):
        if is_unknown(snap.price_usd):
            return UNKNOWN
        dec = self._decimals.get(snap.address, 6)
        amount = int(50 / snap.price_usd * 10**dec)  # ~$50 test sell
        q = await self._quote(snap.address, SOL, max(amount, 1))
        if is_unknown(q):
            return UNKNOWN
        if "_status" in q:
            body = str(q["_body"])
            return False if any(code in body for code in NO_ROUTE_CODES) else UNKNOWN
        return True

    async def quote_impacts(self, snap: TokenSnapshot, usd: float):
        """Size impact for buying ``usd`` and selling that size back.

        Jupiter's priceImpactPct is not size-sensitive in practice (it mostly reflects
        fees), so impact is measured: the full-size quote's rate vs a small reference
        quote's rate on the same route. Proportional fees cancel; what remains is impact.
        """
        sol = await self._sol_price()
        if is_unknown(sol) or is_unknown(snap.price_usd) or snap.price_usd <= 0:
            return UNKNOWN, UNKNOWN
        dec = self._decimals.get(snap.address)
        if dec is None:
            report = await self._rugcheck(snap.address)
            if is_unknown(report) or "decimals" not in (report.get("token") or {}):
                return UNKNOWN, UNKNOWN
            dec = self._decimals[snap.address] = int(report["token"]["decimals"])
        ref_usd = min(2.0, usd / 10)

        async def out(inp: str, outp: str, amount: int):
            q = await self._quote(inp, outp, max(amount, 1))
            if is_unknown(q) or "_status" in q:
                return None
            return _f(q.get("outAmount"))

        lamports = lambda u: int(u / sol * 1e9)
        units = lambda u: int(u / snap.price_usd * 10**dec)
        buy_ref, buy_full = await out(SOL, snap.address, lamports(ref_usd)), await out(SOL, snap.address, lamports(usd))
        sell_ref, sell_full = await out(snap.address, SOL, units(ref_usd)), await out(snap.address, SOL, units(usd))

        def impact(ref, full, scale):
            if not ref or full is None or is_unknown(ref) or is_unknown(full):
                return UNKNOWN
            return max(0.0, (1 - (full / scale) / ref) * 100)

        return impact(buy_ref, buy_full, usd / ref_usd), impact(sell_ref, sell_full, usd / ref_usd)

    async def market_context(self) -> MarketContext:
        await self._sol_price()
        return MarketContext(
            base_coin_trend_24h_pct=getattr(self, "_sol_change", UNKNOWN),
            meme_sector_volume_trend_pct=UNKNOWN,
            hour_utc=self.now().hour,
        )
