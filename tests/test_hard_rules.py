"""One test (at least) for every hard rule in the master prompt."""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from conftest import NOW, market, safety, snapshot
from memebot.broker import LIVE_PHRASE, ExecutionError, make_broker
from memebot.cli import main as cli_main
from memebot.config import load_config
from memebot.entry import Entry, position_size
from memebot.gatekeeper import Gatekeeper
from memebot.loop import Orchestrator, PendingPlan
from memebot.models import EntryBlock, GatekeeperBlock, Record, iso
from memebot.portfolio import Fill, Position, RiskManager, RuleViolation
from memebot.scout import Scout


def make_pos(addr="A", cost=20.0, price=1.0, stop=0.8, target=1.5, opened=NOW) -> Position:
    tokens = cost / price
    return Position(
        address=addr, symbol=addr, opened_at=opened, tier="A", size_multiplier=1.0, planned_size_usd=cost,
        stop_price=stop, initial_stop=stop, first_target=target, setup="range_base",
        tranche_plan=[{"pct": 50}, {"pct": 50}], entry_zone=[price * 0.97, price * 1.02],
        planned_risk_usd=cost * (price - stop) / price,
        fills=[Fill(opened, "buy", tokens, price, cost, 0.06, 0.1, 3.0)], highest_close=price,
    )


# --------------------------------------------------------------- never trade a REJECT


def test_entry_never_overrides_gatekeeper_reject(cfg):
    rec = Record.from_snapshot(snapshot())
    rec.gatekeeper = GatekeeperBlock("REJECT", ["Mint: mint authority not revoked"], 80, None, 0.0, [], [], [], iso(NOW))
    block = Entry(cfg).plan(rec, snapshot(), NOW, capital=1000, room_left=100)
    assert block.decision == "NO_ENTRY"
    assert "never overrides" in block.reasons[0]


def test_orchestrator_refuses_to_fill_a_rejected_token(cfg, tmp_path):
    from memebot.data.sim import SimulatedMarket

    orch = Orchestrator(cfg, SimulatedMarket(), tmp_path)
    rec = Record.from_snapshot(snapshot())
    rec.gatekeeper = GatekeeperBlock("REJECT", ["x"], 90, None, 0.0, [], [], [], iso(NOW))
    rec.entry = EntryBlock("ENTER", "range_base", [0.7, 0.8], 20, [{"pct": 50}, {"pct": 50}], 0.6, "", 1.0, 2.0, 3, iso(NOW))
    with pytest.raises(RuleViolation):
        orch._open(PendingPlan(rec, NOW, NOW + timedelta(minutes=30), 0.75), snapshot(), NOW, type("R", (), {"fills": 0})())


def test_any_hard_fail_is_reject_regardless_of_score(cfg):
    gk = Gatekeeper(cfg)
    rec = Record.from_snapshot(snapshot())
    block = gk.check(rec, snapshot(), safety(freeze_authority_active=True), market(), NOW)
    assert block.verdict == "REJECT" and block.size_multiplier == 0


# --------------------------------------------------------------- sizing caps


def test_max_position_two_percent_of_capital():
    size, limits = position_size(1000, 0.005, 1.0, 0.05, 10_000_000, 1000)  # tight stop would allow $100
    assert limits["risk_based"] == pytest.approx(100)
    assert size == pytest.approx(20)


def test_spec_size_example_is_twenty_dollars():
    size, limits = position_size(1000, 0.005, 1.0, 0.25, 100_000, 1000)
    assert limits == {"risk_based": 20.0, "position_cap": 20.0, "pool_share": 1000.0, "exposure_room": 1000}
    assert size == 20


def test_position_never_above_one_percent_of_pool():
    size, _ = position_size(1_000_000, 0.005, 1.0, 0.25, 50_000, 1e9)
    assert size == pytest.approx(500)


def test_max_total_exposure_and_max_positions(cfg, tmp_path):
    risk = RiskManager(cfg, tmp_path)
    five = [make_pos(addr=f"T{i}", cost=20) for i in range(5)]
    prices = {p.address: 1.0 for p in five}
    blockers = risk.entry_blockers("NEW", five, prices, NOW)
    assert any("max open positions" in b for b in blockers)
    assert any("exposure cap" in b for b in blockers)
    assert risk.room_left(five, prices) == 0


# --------------------------------------------------------------- loss limits


def test_daily_loss_limit_pauses_entries_for_24h(cfg, tmp_path):
    risk = RiskManager(cfg, tmp_path)
    pos = make_pos(cost=40, price=1.0)
    pos.fills.append(Fill(NOW, "sell", 40, 0.2, 8.0, 0.03, 0.1, 1.0))
    pos.closed_at = NOW
    risk.record_close(pos, NOW)  # -$32 >= 3% of $1000
    alerts = risk.update([], {}, NOW)
    assert any("Daily loss limit" in a for a in alerts)
    assert any("daily loss" in b for b in risk.entry_blockers("X", [], {}, NOW + timedelta(hours=23)))
    assert not any("daily loss" in b for b in risk.entry_blockers("X", [], {}, NOW + timedelta(hours=24, minutes=1)))


def test_drawdown_breaker_halts_until_human_review(cfg, tmp_path):
    risk = RiskManager(cfg, tmp_path / "state")
    risk.peak_equity = 1000
    risk.realized_pnl = -160  # 16% down from peak
    alerts = risk.update([], {}, NOW)
    assert any("Drawdown breaker" in a for a in alerts)
    assert risk.drawdown_halted()
    assert any("drawdown" in b for b in risk.entry_blockers("X", [], {}, NOW + timedelta(days=3)))
    # resume without --reviewed is refused
    assert cli_main(["--dir", str(tmp_path), "resume"]) == 1
    assert risk.drawdown_halted()
    assert cli_main(["--dir", str(tmp_path), "resume", "--reviewed"]) == 0
    assert not risk.drawdown_halted()


# --------------------------------------------------------------- stops and averaging


def test_stop_never_moves_further_away():
    pos = make_pos(stop=0.8)
    pos.move_stop(0.9)
    assert pos.stop_price == 0.9
    with pytest.raises(RuleViolation):
        pos.move_stop(0.85)
    assert pos.stop_price == 0.9


def test_never_average_down():
    pos = make_pos(price=1.0)
    with pytest.raises(RuleViolation):
        pos.check_add_allowed(0.95)
    pos.check_add_allowed(1.05)  # adding to a winner is allowed


def test_tranche_two_skipped_while_first_half_is_losing(cfg, tmp_path):
    from memebot.data.sim import SimulatedMarket
    from memebot.loop import TickReport

    from conftest import candles

    orch = Orchestrator(cfg, SimulatedMarket(), tmp_path)
    pos = make_pos(cost=10, price=1.0, opened=NOW - timedelta(minutes=30))
    pos.planned_size_usd = 20  # tranche 1 = $10 of a $20 plan
    pos.tranches_filled = 1
    orch.positions.append(pos)
    held_zone = candles([0.99] * 6, NOW - timedelta(minutes=30))  # inside the zone, but below avg entry
    orch._maybe_tranche_two(pos, snapshot(price_usd=0.99, candles_5m=held_zone), NOW, TickReport(NOW))
    assert len(pos.fills) == 1 and pos.tranches_filled == 1
    # once the trade proves itself, tranche 2 is allowed
    proven = candles([1.01, 1.03, 1.05, 1.06, 1.08, 1.1], NOW - timedelta(minutes=30))
    orch._maybe_tranche_two(pos, snapshot(price_usd=1.1, candles_5m=proven), NOW, TickReport(NOW))
    assert len(pos.fills) == 2 and pos.tranches_filled == 2


# --------------------------------------------------------------- manipulation


def test_scout_drops_paid_or_new_account_buzz(cfg):
    res = Scout(cfg).evaluate(snapshot(social_paid_or_new_only=True), NOW, {})
    assert not res.passed and any("paid promoters" in d for d in res.dropped)


def test_wash_trading_raises_volume_authenticity_risk(cfg):
    gk = Gatekeeper(cfg)
    clean, _ = gk.risk_categories(snapshot(unique_traders_1h=600), safety(), market(), NOW)
    washy, _ = gk.risk_categories(snapshot(unique_traders_1h=40), safety(), market(), NOW)
    assert washy["volume_authenticity"] > clean["volume_authenticity"]


# --------------------------------------------------------------- paper by default


def test_paper_mode_is_default(cfg):
    assert cfg.mode == "paper"
    assert make_broker(cfg).mode == "paper"


def test_live_requires_typed_phrase(tmp_path):
    cfg = load_config(overrides={"mode": "live"})
    with pytest.raises(ExecutionError):
        make_broker(cfg, "yes")
    with pytest.raises(ExecutionError):  # even with the phrase, live is not implemented in this build
        make_broker(cfg, LIVE_PHRASE)


def test_config_refuses_slippage_above_five(tmp_path):
    with pytest.raises(ValueError):
        load_config(overrides={"entry": {"max_slippage_pct": 6}})


# --------------------------------------------------------------- kill switch


def test_kill_switch_stop_blocks_entries(cfg, tmp_path):
    risk = RiskManager(cfg, tmp_path)
    risk.stop_entries()
    assert any("kill switch" in b for b in risk.entry_blockers("X", [], {}, NOW))
    risk.resume_entries()
    assert risk.entry_blockers("X", [], {}, NOW) == []


async def test_kill_switch_exit_all_flattens_everything(cfg, tmp_path):
    from memebot.data.sim import SimulatedMarket

    sim = SimulatedMarket()
    orch = Orchestrator(cfg, sim, tmp_path)
    for _ in range(3):
        await orch.tick()
        sim.advance(1)
    assert any(p.is_open for p in orch.positions), "sim should have opened positions"
    orch.risk.request_exit_all()
    await orch.tick()
    assert not any(p.is_open for p in orch.positions)
    assert all(p.exit_trigger == "kill_switch" for p in orch.closed)
    assert orch.risk.entries_stopped()
    assert not orch.risk.exit_all_requested()


# --------------------------------------------------------------- data failure


async def test_data_failure_blocks_entries_but_exits_keep_running(cfg, tmp_path):
    from memebot.data.sim import SimulatedMarket

    sim = SimulatedMarket()
    orch = Orchestrator(cfg, sim, tmp_path)
    await orch.tick()
    opened = [p for p in orch.positions if p.is_open]
    assert opened

    async def broken(*_a, **_k):
        raise ConnectionError("api down")

    sim.discover = broken
    sim.safety = broken
    n_before = len(orch.positions) + len(orch.closed)
    for _ in range(80):  # FROGLORD rugs at minute 45
        sim.advance(1)
        await orch.tick()
    assert len(orch.positions) + len(orch.closed) == n_before, "no new entries while data is failing"
    assert any(p.exit_trigger == "emergency" for p in orch.closed), "exits still ran"
