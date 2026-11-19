"""End-to-end paper run on the simulated market."""
from __future__ import annotations

import json

from memebot.data.sim import SimulatedMarket
from memebot.loop import Orchestrator


async def test_full_paper_run(cfg, tmp_path):
    sim = SimulatedMarket()
    orch = Orchestrator(cfg, sim, tmp_path, journal_seed=1)
    by_symbol = {}
    for t in sim.tokens.values():
        by_symbol.setdefault(t.symbol, []).append(t.address)

    for _ in range(700):
        await orch.tick()
        sim.advance(1)

    traded = {p.symbol for p in orch.closed} | {p.symbol for p in orch.positions}
    # traps never traded
    assert not traded & {"HONEYCAT", "MINTDOG", "WHALEAI", "TINYCAT"}
    copycat = by_symbol["PURRAI"][1]
    assert all(p.address != copycat for p in orch.closed + orch.positions)

    triggers = {p.symbol: p.exit_trigger for p in orch.closed}
    assert triggers.get("FROGLORD") == "emergency"  # the rug
    purr = next(p for p in orch.closed if p.symbol == "PURRAI")
    assert purr.first_target_done and purr.realized_pnl() > 0

    # every position respects the 2% cap and exposure cap
    for p in orch.closed:
        assert p.cost_usd <= cfg.capital_usd * cfg.risk.max_position_pct / 100 + 1e-6

    # journal written: decisions, trades, review
    jdir = tmp_path / cfg.loop.journal_dir
    rows = [json.loads(l) for l in (jdir / "trades.jsonl").read_text().splitlines()]
    assert len(rows) == len(orch.closed)
    assert all({"r_multiple", "fees_usd", "actual_sell_impact_pct", "record"} <= set(r) for r in rows)
    review = orch.journal.weekly_review()
    assert review["trades"] == len(rows)
    assert review["rugs_or_honeypots_that_passed"]  # FROGLORD shows up for a new hard-fail rule
    bots = {json.loads(l)["bot"] for l in (jdir / "decisions.jsonl").read_text().splitlines()}
    assert {"scout", "gatekeeper", "entry", "exit"} <= bots


async def test_state_survives_restart(cfg, tmp_path):
    sim = SimulatedMarket()
    orch = Orchestrator(cfg, sim, tmp_path)
    await orch.tick()
    open_syms = sorted(p.symbol for p in orch.positions)
    assert open_syms
    again = Orchestrator(cfg, sim, tmp_path)
    assert sorted(p.symbol for p in again.positions) == open_syms
    assert again.positions[0].fills and again.positions[0].stop_price > 0
