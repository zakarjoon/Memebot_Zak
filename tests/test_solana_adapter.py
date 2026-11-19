"""Parsing of the free Solana endpoints, using mocked responses in their documented shapes.
These do not prove the live APIs still look like this — confirm before relying on them."""
from __future__ import annotations

import time
from datetime import datetime, timezone

import httpx

from memebot.config import load_config
from memebot.data.solana_free import SOL, SolanaFreeProvider
from memebot.gatekeeper import Gatekeeper
from memebot.models import UNKNOWN, MarketContext, Record

MINT = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"
POOL = "Pool1111111111111111111111111111111111111111"


def pair(addr=MINT, symbol="EXAMPLE", liq=120000.0, price="0.00042"):
    return {
        "chainId": "solana", "pairAddress": POOL, "baseToken": {"address": addr, "name": "Example", "symbol": symbol},
        "priceUsd": price, "liquidity": {"usd": liq}, "marketCap": 900000, "fdv": 900000,
        "volume": {"h1": 60000, "h24": 700000}, "priceChange": {"h24": 140.0},
        "txns": {"h1": {"buys": 500, "sells": 300}}, "pairCreatedAt": int((time.time() - 3 * 3600) * 1000),
    }


def handler(fail_rugcheck=False, no_route=False):
    now = int(time.time())
    def h(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "token-profiles" in url or "token-boosts" in url:
            return httpx.Response(200, json=[{"chainId": "solana", "tokenAddress": MINT}, {"chainId": "ethereum", "tokenAddress": "0xabc"}])
        if "trending_pools" in url or "new_pools" in url:
            return httpx.Response(200, json={"data": [{"relationships": {"base_token": {"data": {"id": f"solana_{MINT}"}}}}]})
        if f"/tokens/v1/solana/{SOL}" in url:
            return httpx.Response(200, json=[{"priceUsd": "150", "liquidity": {"usd": 9e6}, "priceChange": {"h24": 1.5}}])
        if "/tokens/v1/solana/" in url:
            return httpx.Response(200, json=[pair(liq=5000.0), pair()])
        if "ohlcv" in url:
            rows = [[now - 300 * i, 0.0004, 0.00043, 0.00039, 0.00042, 5000] for i in range(1, 40)]
            rows.insert(0, [now - 60, 0.0004, 0.0004, 0.0004, 0.0004, 1])  # still-forming candle
            return httpx.Response(200, json={"data": {"attributes": {"ohlcv_list": rows}}})
        if "/info" in url:
            return httpx.Response(200, json={"data": {"attributes": {"holders": {"count": 1450}}}})
        if "rugcheck" in url:
            if fail_rugcheck:
                return httpx.Response(503)
            return httpx.Response(200, json={
                "token": {"decimals": 6, "mintAuthority": None, "freezeAuthority": None},
                "mintAuthority": None, "freezeAuthority": None, "creator": "Dev1",
                "transferFee": {"pct": 0, "authority": "11111111111111111111111111111111"},
                "markets": [{"pubkey": POOL, "lp": {"lpLockedPct": 100}}],
                "topHolders": [{"owner": POOL, "address": "x", "pct": 30.0, "insider": False},
                               {"owner": "Dev1", "address": "y", "pct": 3.0, "insider": True},
                               {"owner": "W2", "address": "z", "pct": 2.0, "insider": False}],
                "risks": [{"name": "Low amount of LP Providers", "level": "warn", "description": ""}],
                "totalHolders": 1450,
            })
        if "/search" in url:
            return httpx.Response(200, json={"pairs": [pair()]})
        if "jup.ag" in url:
            if no_route:
                return httpx.Response(400, json={"error": "No routes found", "errorCode": "COULD_NOT_FIND_ANY_ROUTE"})
            return httpx.Response(200, json={"priceImpactPct": "0.0042"})
        return httpx.Response(404)
    return h


def provider(**kw):
    cfg = load_config(overrides={"data": {"retries": 1, "backoff_base_sec": 0.0, "rate_limits_per_min": {"api.geckoterminal.com": 100000}}})
    return SolanaFreeProvider(cfg, transport=httpx.MockTransport(handler(**kw)))


async def test_discover_and_snapshot_parse():
    p = provider()
    addrs, _ = await p.discover()
    assert addrs == [MINT]  # deduped, non-Solana ignored
    snaps = await p.snapshots(addrs, full=True)
    s = snaps[MINT]
    assert s.liquidity_usd == 120000.0  # deepest pool chosen
    assert s.price_usd == 0.00042 and s.buys_1h == 500 and s.holders == 1450
    assert 170 < s.age_min < 190
    assert len(s.candles_5m) == 39  # forming candle dropped
    assert s.candles_5m[0].ts < s.candles_5m[-1].ts
    assert s.unique_traders_1h == UNKNOWN and s.social_mentions_1h == UNKNOWN
    await p.close()


async def test_safety_parse_and_gatekeeper():
    p = provider()
    s = (await p.snapshots([MINT], True))[MINT]
    rep = await p.safety(s)
    assert rep.mint_authority_active is False and rep.freeze_authority_active is False
    assert rep.sell_tax_pct == 0.0 and rep.tax_mutable is False
    assert rep.lp_locked_pct == 100 and rep.lp_lock_days_remaining == float("inf")
    assert rep.sell_simulation_ok is True and rep.impersonation is False
    assert rep.top_holders[0].excluded  # LP pool excluded
    assert rep.dev_holding_pct == 3.0 and rep.insider_supply_pct == 3.0
    block = Gatekeeper(p.cfg).check(Record.from_snapshot(s), s, rep, MarketContext(1.5, UNKNOWN, 12), datetime.now(timezone.utc))
    assert block.verdict != "REJECT", block.hard_fails
    assert "social_authenticity" in block.category_scores and block.unknowns  # free data leaves gaps
    buy, sell = await p.quote_impacts(s, 20)
    assert round(buy, 2) == 0.42 and round(sell, 2) == 0.42
    await p.close()


async def test_source_failure_is_unknown_and_rejected():
    p = provider(fail_rugcheck=True)
    s = (await p.snapshots([MINT], True))[MINT]
    rep = await p.safety(s)
    assert rep.mint_authority_active == UNKNOWN
    block = Gatekeeper(p.cfg).check(Record.from_snapshot(s), s, rep, MarketContext(), datetime.now(timezone.utc))
    assert block.verdict == "REJECT" and any("Unknown critical" in f for f in block.hard_fails)
    await p.close()


async def test_no_sell_route_is_honeypot():
    p = provider(no_route=True)
    s = (await p.snapshots([MINT], True))[MINT]
    rep = await p.safety(s)
    assert rep.sell_simulation_ok is False
    await p.close()
