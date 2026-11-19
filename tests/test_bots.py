"""Behaviour of each bot against the spec."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from conftest import ADDR, NOW, candles, market, range_base_candles, safety, snapshot
from memebot.entry import Entry
from memebot.exit import Exit
from memebot.gatekeeper import Gatekeeper
from memebot.journal import review_trades
from memebot.models import UNKNOWN, GatekeeperBlock, Holder, Record, iso
from memebot.portfolio import Fill, Position
from memebot.scout import Scout

# ===================================================================== Scout


def good_signals(**kw):
    base = dict(proven_wallets_holding=2, proven_wallets_selling=False, social_mentions_1h=400,
                social_mentions_peak_1h=420, social_distinct_aged_accounts=80, social_paid_or_new_only=False)
    base.update(kw)
    return base


@pytest.mark.parametrize("field,value,needle", [
    ("liquidity_usd", 40_000, "liquidity_usd"),
    ("age_min", 20, "age_min"),
    ("vol_1h_usd", 10_000, "vol_1h_usd"),
    ("holders", 250, "holders"),
    ("liquidity_usd", UNKNOWN, "UNKNOWN"),
])
def test_scout_minimum_filters(cfg, field, value, needle):
    res = Scout(cfg).evaluate(snapshot(**{field: value}, **good_signals()), NOW, {})
    assert not res.passed
    assert any(needle in d for d in res.dropped)


def test_scout_skips_late_10x_without_a_range(cfg):
    res = Scout(cfg).evaluate(snapshot(price_change_24h_pct=1500, **good_signals()), NOW, {})
    assert any("late buyers" in d for d in res.dropped)


def test_scout_copycat_and_fake_v2(cfg):
    sc = Scout(cfg)
    other = "So11111111111111111111111111111111111111112"
    assert any("copycat" in d for d in sc.evaluate(snapshot(**good_signals()), NOW, {"EXAMPLE": other}).dropped)
    assert any("copycat" in d for d in sc.evaluate(snapshot(name="Example Cat V2", **good_signals()), NOW, {}).dropped)


def test_scout_unverifiable_contract(cfg):
    res = Scout(cfg).evaluate(snapshot(address="not-a-mint", **good_signals()), NOW, {})
    assert any("cannot be verified" in d for d in res.dropped)


def test_scout_scores_and_queues(cfg):
    sc = Scout(cfg)
    rich = sc.evaluate(snapshot(**good_signals()), NOW, {})
    assert rich.passed and rich.block.queue == "priority" and rich.block.potential_score >= 75
    assert set(rich.block.factor_scores) == {"momentum", "holder_growth", "proven_wallets", "narrative", "social", "liquidity"}
    assert rich.block.who_buys_next and rich.block.why_up

    # free-data reality: no wallet / social data -> those factors score 0, never neutral
    bare = sc.evaluate(snapshot(), NOW, {})
    assert "proven_wallets" in bare.block.unknowns and "social" in bare.block.unknowns if bare.passed else True
    if bare.passed:
        assert bare.block.factor_scores["social"] == 0 and bare.block.queue == "watchlist"


def test_scout_must_answer_who_buys_next(cfg):
    s = snapshot(buys_1h=100, sells_1h=300, proven_wallets_holding=0, **{k: v for k, v in good_signals().items() if k != "proven_wallets_holding"})
    s.holder_history = [(NOW - timedelta(minutes=300 - 10 * i), 1500 - i) for i in range(31)]
    res = Scout(cfg).evaluate(s, NOW, {})
    assert not res.passed


def test_scout_returns_at_most_five(cfg):
    snaps = []
    for i in range(8):
        a = ADDR[:-2] + "ab"[i % 2] + "cdefghjk"[i]
        snaps.append(snapshot(address=a, symbol=f"T{i}", **good_signals()))
    records, _ = Scout(cfg).scan(snaps, NOW, {})
    assert len(records) == 5
    scores = [r.scout.potential_score for r in records]
    assert scores == sorted(scores, reverse=True)


# ===================================================================== Gatekeeper


@pytest.mark.parametrize("override,needle", [
    ({"sell_simulation_ok": False}, "Honeypot"),
    ({"sell_tax_pct": 12.0}, "Taxes"),
    ({"tax_mutable": True}, "Taxes"),
    ({"mint_authority_active": True}, "Mint"),
    ({"freeze_authority_active": True}, "Freeze"),
    ({"lp_locked_pct": 50.0}, "Liquidity"),
    ({"lp_lock_days_remaining": 10}, "Liquidity"),
    ({"upgradeable": True}, "Upgradeable"),
    ({"deployer_rugged_before": True}, "Deployer"),
    ({"impersonation": True}, "Impersonation"),
    ({"mint_authority_active": UNKNOWN}, "Unknown critical"),
])
def test_gatekeeper_hard_fails(cfg, override, needle):
    block = Gatekeeper(cfg).check(Record.from_snapshot(snapshot()), snapshot(), safety(**override), market(), NOW)
    assert block.verdict == "REJECT"
    assert any(needle in f for f in block.hard_fails)


def test_gatekeeper_whale_excludes_lp_and_merges_clusters(cfg):
    gk = Gatekeeper(cfg)
    # LP pool holds 25% but is excluded -> no whale
    assert gk.check(Record.from_snapshot(snapshot()), snapshot(), safety(), market(), NOW).verdict != "REJECT"
    # two 6% wallets funded from one source count as one 12% holder
    holders = [Holder("LP", 25, True), Holder("a", 6), Holder("b", 6)] + [Holder(f"w{i}", 1) for i in range(8)]
    rep = safety(top_holders=holders, holder_clusters=[["a", "b"]])
    block = gk.check(Record.from_snapshot(snapshot()), snapshot(), rep, market(), NOW)
    assert block.verdict == "REJECT" and any("Whale" in f for f in block.hard_fails)


@pytest.mark.parametrize("score,verdict,tier,mult", [
    (0, "PASS", "A", 1.0), (30, "PASS", "A", 1.0), (31, "PASS", "B", 0.6), (50, "PASS", "B", 0.6),
    (51, "CAUTION", "C", 0.3), (65, "CAUTION", "C", 0.3), (66, "REJECT", None, 0.0), (100, "REJECT", None, 0.0),
])
def test_gatekeeper_tiers(cfg, score, verdict, tier, mult):
    t = Gatekeeper(cfg).tier_for(score)
    assert (t["verdict"], t["tier"], t["size_multiplier"]) == (verdict, tier, mult)


def test_gatekeeper_unknowns_count_as_risk(cfg):
    gk = Gatekeeper(cfg)
    full = gk.check(Record.from_snapshot(snapshot()), snapshot(), safety(), market(), NOW)
    blind = gk.check(Record.from_snapshot(snapshot()), snapshot(), safety(bot_followers=UNKNOWN, copied_website=UNKNOWN,
                     deployer_age_days=UNKNOWN), market(meme_sector_volume_trend_pct=UNKNOWN), NOW)
    assert blind.risk_score > full.risk_score
    assert blind.unknowns and blind.confidence != "high"


def test_gatekeeper_pass_lists_thesis_killers(cfg):
    block = Gatekeeper(cfg).check(Record.from_snapshot(snapshot()), snapshot(), safety(), market(), NOW)
    assert block.verdict in ("PASS", "CAUTION")
    assert 3 <= len(block.thesis_killers) <= 5
    assert len(block.top_risks) == 3


# ===================================================================== Entry


def passed_record(cfg, tier="A", mult=1.0, verdict="PASS", checked=NOW):
    rec = Record.from_snapshot(snapshot())
    rec.gatekeeper = GatekeeperBlock(verdict, [], 20, tier, mult, ["r"], ["k"], [], iso(checked))
    return rec


def test_entry_range_base_plan(cfg):
    rec = passed_record(cfg)
    block = Entry(cfg).plan(rec, snapshot(), NOW, capital=1000, room_left=100)
    assert block.decision == "ENTER", block.reasons
    assert block.setup == "range_base"
    lo, hi = block.entry_zone
    assert lo < hi and block.stop_price < lo
    assert block.reward_to_risk >= 2.0
    assert block.size_usd <= 20
    assert [t["pct"] for t in block.tranches] == [50, 50]
    assert block.max_slippage_pct <= 5
    assert block.expires_at == iso(NOW + timedelta(minutes=30))


def test_entry_preflight_quotes(cfg):
    e = Entry(cfg)
    rec = passed_record(cfg)
    e.plan(rec, snapshot(), NOW, capital=1000, room_left=100)
    assert e.preflight(rec, 0.5, 4.0).decision == "NO_ENTRY"  # cannot get out
    rec2 = passed_record(cfg)
    e.plan(rec2, snapshot(), NOW, capital=1000, room_left=100)
    assert e.preflight(rec2, UNKNOWN, 1.0).decision == "NO_ENTRY"
    rec3 = passed_record(cfg)
    e.plan(rec3, snapshot(), NOW, capital=1000, room_left=100)
    assert e.preflight(rec3, 0.2, 0.4).decision == "ENTER"


def test_entry_stale_verdict_or_data(cfg):
    e = Entry(cfg)
    assert e.plan(passed_record(cfg, checked=NOW - timedelta(minutes=20)), snapshot(), NOW, capital=1000, room_left=100).decision == "NO_ENTRY"
    stale = snapshot(data_timestamp=NOW - timedelta(minutes=6))
    assert e.plan(passed_record(cfg), stale, NOW, capital=1000, room_left=100).decision == "NO_ENTRY"


def test_entry_do_not_chase_or_enter_on_selling(cfg):
    e = Entry(cfg)
    cs, _ = range_base_candles()
    chase = snapshot(price_usd=1.5, candles_5m=cs)
    assert any("15 min" in r for r in e.plan(passed_record(cfg), chase, NOW, capital=1000, room_left=100).reasons)
    selling = snapshot(buys_1h=100, sells_1h=200)
    assert any("buy/sell" in r for r in e.plan(passed_record(cfg), selling, NOW, capital=1000, room_left=100).reasons)


def test_entry_tier_c_needs_three_to_one(cfg):
    block = Entry(cfg).plan(passed_record(cfg, tier="C", mult=0.3, verdict="CAUTION"), snapshot(), NOW, capital=1000, room_left=100)
    if block.decision == "ENTER":
        assert block.reward_to_risk >= 3.0
    else:
        assert any("3.0" in r for r in block.reasons)


def test_entry_no_setup_means_no_entry(cfg):
    flat = candles([1.0] * 40, NOW - timedelta(minutes=200), vols=[1000.0] * 40)
    block = Entry(cfg).plan(passed_record(cfg), snapshot(price_usd=1.0, candles_5m=flat), NOW, capital=1000, room_left=100)
    assert block.decision == "NO_ENTRY"


def test_spec_handoff_example_is_internally_consistent(cfg):
    """$1,000 capital, Tier B, zone 0.00041-0.00044, stop 0.00032 -> size $12, R:R 2.4."""
    from memebot.entry import Setup, position_size

    s = Setup("pullback_to_support", (0.00041, 0.00044), 0.00032, 0.00068, "")
    size, _ = position_size(1000, 0.005, 0.6, s.stop_distance, 120_000, 1000)
    assert round(size) == 12
    assert round(s.reward_to_risk, 1) == 2.4


# ===================================================================== Exit


def open_pos(price=1.0, stop=0.85, target=1.3, opened=None) -> Position:
    opened = opened or NOW - timedelta(minutes=60)
    return Position(
        address=ADDR, symbol="EX", opened_at=opened, tier="A", size_multiplier=1.0, planned_size_usd=20,
        stop_price=stop, initial_stop=stop, first_target=target, setup="range_base", tranche_plan=[{"pct": 100}],
        entry_zone=[0.98, 1.02], planned_risk_usd=3.0,
        fills=[Fill(opened, "buy", 20 / price, price, 20, 0.06, 0.1, 3)], highest_close=price,
        dev_holding_at_entry=2.0, entry_sell_tax_pct=0.0, entry_insiders_selling=False,
    )


def exit_snap(prices, **kw):
    start = NOW - timedelta(minutes=5 * len(prices))
    cs = candles(prices, start)
    s = snapshot(price_usd=prices[-1], candles_5m=cs, **kw)
    s.candles_15m = candles(prices[::3], start, 15)
    s.liquidity_history = [(NOW - timedelta(minutes=10 - i), 120_000) for i in range(10)]
    return s


def test_exit_emergency_beats_profit_ladder(cfg):
    pos = open_pos()
    s = exit_snap([1.0, 1.1, 1.2, 1.4], liquidity_usd=80_000)  # target hit AND liquidity -33%
    d = Exit(cfg).evaluate(pos, s, safety(), None, NOW)
    assert d.block.action == "EXIT" and d.block.priority == 1 and d.emergency


@pytest.mark.parametrize("override", [
    {"sell_simulation_ok": False}, {"mint_authority_active": True}, {"freeze_authority_active": True},
    {"sell_tax_pct": 5.0}, {"dev_holding_pct": 1.0},  # dev sold 50% of holdings
])
def test_exit_emergency_triggers(cfg, override):
    d = Exit(cfg).evaluate(open_pos(), exit_snap([1.0, 1.02, 1.03]), safety(**override), None, NOW)
    assert d.block.trigger == "emergency" and d.block.pct_of_position == 100


def test_exit_recheck_reject_is_emergency(cfg):
    rej = GatekeeperBlock("REJECT", ["Freeze: freeze authority active"], 90, None, 0, [], [], [], iso(NOW))
    d = Exit(cfg).evaluate(open_pos(), exit_snap([1.0, 1.02]), safety(), rej, NOW)
    assert d.block.priority == 1


def test_exit_invalidation_on_5m_close_below_stop(cfg):
    d = Exit(cfg).evaluate(open_pos(stop=0.9), exit_snap([1.0, 0.95, 0.88]), safety(), None, NOW)
    assert d.block.trigger == "invalidation" and d.block.action == "EXIT"


def test_exit_profit_ladder_sequence(cfg):
    ex = Exit(cfg)
    pos = open_pos(target=1.3)
    d = ex.evaluate(pos, exit_snap([1.0, 1.15, 1.31]), safety(), None, NOW)
    assert d.block.trigger == "profit_ladder_first_target"
    assert d.tokens_to_sell == pytest.approx(pos.tokens_bought * 0.30)
    assert d.new_stop == pytest.approx(pos.avg_entry)  # break-even
    for f in d.mark_flags:
        setattr(pos, f, True)
    pos.fills.append(Fill(NOW, "sell", d.tokens_to_sell, 1.31, d.tokens_to_sell * 1.31, 0.02, 0.1, 1))
    pos.move_stop(d.new_stop)

    d2 = ex.evaluate(pos, exit_snap([1.3, 1.6, 2.05]), safety(), None, NOW)
    assert d2.block.trigger == "profit_ladder_2x"
    pos.fills.append(Fill(NOW, "sell", d2.tokens_to_sell, 2.05, d2.tokens_to_sell * 2.05, 0.02, 0.1, 1))
    assert pos.proceeds_usd >= pos.cost_usd  # full cost recovered
    for f in d2.mark_flags:
        setattr(pos, f, True)

    d3 = ex.evaluate(pos, exit_snap([2.0, 2.5, 3.1]), safety(), None, NOW)
    assert d3.block.trigger == "profit_ladder_3x"
    pos.fills.append(Fill(NOW, "sell", d3.tokens_to_sell, 3.1, d3.tokens_to_sell * 3.1, 0.02, 0.1, 1))
    assert pos.tokens >= pos.tokens_bought * 0.20 - 1e-9  # runner kept


def test_exit_trailing_stop_on_runner(cfg):
    pos = open_pos(target=1.3)
    pos.first_target_done = True
    pos.stop_price = 1.0
    d = Exit(cfg).evaluate(pos, exit_snap([1.0, 1.5, 2.0, 1.9, 1.35]), safety(), None, NOW)
    assert d.block.trigger == "trailing_stop"


def test_exit_time_stop(cfg):
    pos = open_pos(target=1.5, opened=NOW - timedelta(hours=4, minutes=5))
    d = Exit(cfg).evaluate(pos, exit_snap([1.0, 1.01, 0.99, 1.0, 1.02, 1.0]), safety(), None, NOW)
    assert d.block.trigger == "time_stop" and d.block.priority == 6


def test_exit_decay_one_signal_trims_two_exits(cfg):
    ex = Exit(cfg)
    pos = open_pos(target=2.0)
    s = exit_snap([1.0, 1.05, 1.04, 1.06, 1.05, 1.07], proven_wallets_selling=True)
    d = ex.evaluate(pos, s, safety(), None, NOW)
    assert d.block.action == "TRIM" and d.block.pct_of_position == 50 and d.new_stop >= pos.stop_price
    s2 = exit_snap([1.0, 1.05, 1.04, 1.06, 1.05, 1.07], proven_wallets_selling=True, social_mentions_1h=100, social_mentions_peak_1h=400)
    d2 = ex.evaluate(open_pos(target=2.0), s2, safety(), None, NOW)
    assert d2.block.action == "EXIT" and d2.block.trigger == "momentum_decay"


def test_exit_unknown_price_holds(cfg):
    d = Exit(cfg).evaluate(open_pos(), snapshot(price_usd=UNKNOWN), safety(), None, NOW)
    assert d.block.action == "HOLD"


# ===================================================================== Journal / handoff


def test_expectancy_formula(cfg):
    trades = [{"pnl_usd": 10, "r_multiple": 2.0, "closed_at": "a", "fees_usd": 0, "exit_trigger": "x", "symbol": "a", "token": "a"},
              {"pnl_usd": -5, "r_multiple": -1.0, "closed_at": "b", "fees_usd": 0, "exit_trigger": "y", "symbol": "b", "token": "b"}]
    r = review_trades(trades, [], [], cfg)
    assert r["win_rate"] == 0.5
    assert r["expectancy_r"] == pytest.approx(0.5 * 2.0 - 0.5 * 1.0)
    assert "Not enough trades" in r["suggestion"]


def test_handoff_record_has_spec_shape(cfg):
    rec = passed_record(cfg)
    rec.scout = Scout(cfg).evaluate(snapshot(**good_signals()), NOW, {}).block
    Entry(cfg).plan(rec, snapshot(), NOW, capital=1000, room_left=100)
    d = json.loads(json.dumps(rec.to_dict(), default=str))
    assert set(d["token"]) == {"chain", "address", "symbol", "pair_address", "data_timestamp"}
    assert {"potential_score", "factor_scores", "why_up", "who_buys_next", "metrics", "sources"} <= set(d["scout"])
    assert {"verdict", "hard_fails", "risk_score", "tier", "size_multiplier", "top_risks", "thesis_killers", "unknowns", "checked_at"} <= set(d["gatekeeper"])
    assert {"decision", "setup", "entry_zone", "size_usd", "tranches", "stop_price", "invalidation", "first_target",
            "reward_to_risk", "max_slippage_pct", "expires_at"} <= set(d["entry"])
