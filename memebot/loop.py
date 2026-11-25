"""The paper-trading loop: Scout -> Gatekeeper -> Entry -> Exit -> Journal.

Nothing reaches a trade without passing the Gatekeeper. If a data source fails,
new entries are blocked and exits keep running.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .broker import ExecutionError, PaperBroker
from .config import Section
from .entry import Entry
from .exit import Exit
from .gatekeeper import Gatekeeper
from .journal import Journal
from .models import Record, TokenSnapshot, is_unknown, iso, known
from .portfolio import Fill, Position, RiskManager, RuleViolation
from .scout import Scout

log = logging.getLogger("memebot")

HISTORY_MIN = 6 * 60


@dataclass
class PendingPlan:
    record: Record
    created_at: datetime
    expires_at: datetime
    snapshot_price: float


@dataclass
class TickReport:
    now: datetime
    scanned: int = 0
    scout_passed: int = 0
    gk_verdicts: dict[str, int] = field(default_factory=dict)
    plans: int = 0
    fills: int = 0
    exits: list[str] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)


class Orchestrator:
    def __init__(self, cfg: Section, provider: Any, base_dir: Path | str = ".", broker: Any = None, journal_seed: int | None = None):
        self.cfg = cfg
        self.provider = provider
        base = Path(base_dir)
        self.state_dir = base / cfg.loop.state_dir
        self.broker = broker or PaperBroker(cfg)
        self.journal = Journal(cfg, base / cfg.loop.journal_dir, seed=journal_seed)
        self.risk = RiskManager(cfg, self.state_dir)
        self.scout, self.gatekeeper, self.entry, self.exit = Scout(cfg), Gatekeeper(cfg), Entry(cfg), Exit(cfg)
        self.positions: list[Position] = []
        self.closed: list[Position] = []
        self.plans: dict[str, PendingPlan] = {}
        self.history: dict[str, dict[str, list[tuple[datetime, float]]]] = {}
        self.rejected: dict[str, datetime] = {}  # Gatekeeper REJECTs: discarded, no overrides
        self.last_scan: datetime | None = None
        self.last_monitor: datetime | None = None
        self.last_summary_day: str | None = None
        self.data_ok = True
        self.load_state()

    # ================================================================== tick

    async def tick(self) -> TickReport:
        now = self.provider.now()
        rep = TickReport(now)

        open_pos = [p for p in self.positions if p.is_open]
        snaps = await self._safe(self.provider.snapshots([p.address for p in open_pos], True), {}) if open_pos else {}
        for s in snaps.values():
            self._merge_history(s, now)
        prices = {a: s.price_usd for a, s in snaps.items() if known(s.price_usd)}
        rep.alerts += self.risk.update(self.positions, prices, now)
        for a in rep.alerts:
            log.warning("ALERT %s", a)
            self.journal.decision(now, "risk", "*", {"alert": a}, a)

        if self.risk.exit_all_requested():
            await self._exit_all(snaps, now, rep)
        elif self._due(self.last_monitor, self.cfg.loop.monitor_interval_min, now):
            self.last_monitor = now
            await self._monitor(snaps, now, rep)

        await self._work_plans(now, rep)

        if self._due(self.last_scan, self.cfg.loop.scan_interval_min, now):
            self.last_scan = now
            blockers = self.risk.entry_blockers("*", self.positions, prices, now)
            global_block = [b for b in blockers if "already holding" not in b and "cooldown" not in b]
            if global_block:
                log.info("scan skipped: %s", "; ".join(global_block))
            else:
                await self._scan(now, rep, prices)

        await self._followups(now)
        self._daily_summary(now, prices)
        self.save_state()
        return rep

    async def run(self, ticks: int | None = None, simulated: bool = False) -> None:
        n = 0
        try:
            while ticks is None or n < ticks:
                await self.tick()
                n += 1
                if simulated:
                    self.provider.advance(1)
                else:
                    await asyncio.sleep(max(1.0, 60 - datetime.now().second))
        finally:
            await self.provider.close()

    # ================================================================== scan

    async def _scan(self, now: datetime, rep: TickReport, prices: dict[str, float]) -> None:
        found = await self._safe(self.provider.discover(), None)
        if found is None:
            log.warning("discovery failed — UNKNOWN; no new entries this scan")
            return
        addrs, known_symbols = found
        self.rejected = {a: t for a, t in self.rejected.items() if t > now}
        addrs = [a for a in addrs if a not in self.plans and a not in self.rejected
                 and not any(p.address == a and p.is_open for p in self.positions)]
        light = await self._safe(self.provider.snapshots(addrs, False), {})
        survivors = [a for a, s in light.items() if self._prefilter(s)]
        snaps = await self._safe(self.provider.snapshots(survivors, True), {})
        for s in snaps.values():
            self._merge_history(s, now)
        rep.scanned = len(addrs)

        records, results = self.scout.scan(list(snaps.values()), now, known_symbols)
        rep.scout_passed = len(records)
        self._log_scan(len(addrs), len(survivors), results)
        for r in results:
            if not r.passed:
                self.journal.maybe_sample_rejection(now, r.snapshot.address, r.snapshot.symbol, "scout", r.snapshot.price_usd, r.dropped)
        for rec in records:
            self.journal.decision(now, "scout", rec.token.symbol, rec.to_dict()["scout"], f"score {rec.scout.potential_score} ({rec.scout.queue})")

        market = await self._safe(self.provider.market_context(), None)
        for rec in records:
            snap = snaps[rec.token.address]
            safety = await self._safe(self.provider.safety(snap), None)
            if safety is None or market is None:
                log.warning("%s: safety/market data UNKNOWN — skipped (no entries on unknown data)", rec.token.symbol)
                continue
            gk = self.gatekeeper.check(rec, snap, safety, market, now)
            rep.gk_verdicts[gk.verdict] = rep.gk_verdicts.get(gk.verdict, 0) + 1
            self.journal.decision(now, "gatekeeper", rec.token.symbol, rec.to_dict()["gatekeeper"],
                                  f"{gk.verdict} risk {gk.risk_score}: {'; '.join(gk.hard_fails or gk.top_risks)}")
            log.info("GATEKEEPER %-9s %-8s risk=%s %s", rec.token.symbol, gk.verdict, gk.risk_score, "; ".join(gk.hard_fails[:2]))
            if gk.verdict == "REJECT":
                self.rejected[snap.address] = now + timedelta(hours=self.cfg.gatekeeper.reject_memory_hours)
                self.journal.maybe_sample_rejection(now, snap.address, snap.symbol, "gatekeeper", snap.price_usd, gk.hard_fails or gk.top_risks)
                continue

            blockers = self.risk.entry_blockers(snap.address, self.positions, prices, now)
            if blockers:
                self.journal.decision(now, "entry", rec.token.symbol, {"decision": "NO_ENTRY"}, "; ".join(blockers))
                continue
            plan = self.entry.plan(rec, snap, now, capital=self.risk.capital,
                                   room_left=min(self.risk.room_left(self.positions, prices), self.risk.max_position_usd()),
                                   size_factor=self.risk.size_factor)
            if plan.decision == "ENTER":
                buy_imp, sell_imp = await self._safe(self.provider.quote_impacts(snap, plan.size_usd), ("UNKNOWN", "UNKNOWN"))
                plan = self.entry.preflight(rec, buy_imp, sell_imp)
            self.journal.decision(now, "entry", rec.token.symbol, rec.to_dict()["entry"], "; ".join(plan.reasons))
            log.info("ENTRY      %-9s %-8s %s", rec.token.symbol, plan.decision, "; ".join(plan.reasons)[:160])
            if plan.decision != "ENTER":
                self.journal.maybe_sample_rejection(now, snap.address, snap.symbol, "entry", snap.price_usd, plan.reasons)
                continue
            rep.plans += 1
            self.plans[snap.address] = PendingPlan(rec, now, datetime.fromisoformat(plan.expires_at.replace("Z", "+00:00")), snap.price_usd)
            self._attach_entry_safety(snap.address, safety)

        await self._work_plans(now, rep)

    def _log_scan(self, found: int, survived: int, results) -> None:
        """One readable line per scan, so a quiet market is visibly working, not silent."""
        import re
        from collections import Counter

        drops = Counter()
        for r in results:
            for d in r.dropped[:1]:
                drops[re.sub(r"[\d,.]+", "#", d.split(" — ")[0].split(":")[0])[:40]] += 1
        scored = [((r.block.potential_score if r.passed else _score_of(r)), r.snapshot.symbol) for r in results]
        scored = sorted(x for x in scored if x[0] is not None)
        best = ", ".join(f"{sym} {sc}" for sc, sym in reversed(scored[-3:]))
        log.info("SCAN       found %d, %d past prefilter, %d to Gatekeeper | best: %s | drops: %s",
                 found, survived, sum(r.passed for r in results), best or "-",
                 "; ".join(f"{k} x{v}" for k, v in drops.most_common(3)) or "-")

    def _prefilter(self, s: TokenSnapshot) -> bool:
        """Cheap filters on light data before paying for candles. Unknowns pass to the full check."""
        c = self.cfg.scout
        for value, minimum in ((s.liquidity_usd, c.min_liquidity_usd), (s.age_min, c.min_age_min), (s.vol_1h_usd, c.min_vol_1h_usd)):
            if known(value) and value < minimum:
                return False
        return True

    # ================================================================== entries

    def _attach_entry_safety(self, address: str, safety) -> None:
        self._entry_safety = getattr(self, "_entry_safety", {})
        self._entry_safety[address] = safety

    async def _work_plans(self, now: datetime, rep: TickReport) -> None:
        if not self.plans:
            return
        for addr, plan in list(self.plans.items()):
            if now >= plan.expires_at:
                self.journal.decision(now, "entry", plan.record.token.symbol, {"decision": "EXPIRED"}, "unfilled plan expired; back to Scout")
                del self.plans[addr]
                continue
            snaps = await self._safe(self.provider.snapshots([addr], False), {})
            s = snaps.get(addr)
            if s is None or not known(s.price_usd, s.liquidity_usd):
                continue
            lo, hi = plan.record.entry.entry_zone
            if lo <= s.price_usd <= hi:
                self._open(plan, s, now, rep)
                del self.plans[addr]

    def _open(self, plan: PendingPlan, s: TokenSnapshot, now: datetime, rep: TickReport) -> None:
        rec = plan.record
        e, gk = rec.entry, rec.gatekeeper
        if gk is None or gk.verdict == "REJECT":
            raise RuleViolation("attempted to trade a token the Gatekeeper rejected")
        prices = {p.address: p.avg_entry for p in self.positions if p.is_open}
        blockers = self.risk.entry_blockers(s.address, self.positions, prices, now)
        if blockers:
            self.journal.decision(now, "entry", rec.token.symbol, {"decision": "NO_ENTRY"}, "at fill time: " + "; ".join(blockers))
            return
        size = min(e.size_usd, self.risk.max_position_usd(), self.risk.room_left(self.positions, prices))
        first = round(size * e.tranches[0]["pct"] / 100, 2)
        try:
            fill = self.broker.buy(now, first, s.price_usd, s.liquidity_usd, e.max_slippage_pct, "tranche 1: price in zone")
        except ExecutionError as exc:
            self.journal.decision(now, "entry", rec.token.symbol, {"decision": "NO_ENTRY"}, str(exc))
            return
        mid = sum(e.entry_zone) / 2
        safety = getattr(self, "_entry_safety", {}).get(s.address)
        pos = Position(
            address=s.address, symbol=s.symbol, opened_at=now, tier=gk.tier, size_multiplier=gk.size_multiplier,
            planned_size_usd=size, stop_price=e.stop_price, initial_stop=e.stop_price, first_target=e.first_target,
            setup=e.setup, tranche_plan=e.tranches, entry_zone=e.entry_zone,
            planned_risk_usd=size * (mid - e.stop_price) / mid, record=rec.to_dict(),
            scout_factors=rec.scout.factor_scores if rec.scout else {},
            fills=[fill], tranches_filled=1, highest_close=fill.price,
            dev_holding_at_entry=safety.dev_holding_pct if safety else "UNKNOWN",
            entry_sell_tax_pct=safety.sell_tax_pct if safety else "UNKNOWN",
            entry_insiders_selling=safety.insiders_selling if safety else "UNKNOWN",
            last_recheck_at=now,
        )
        self.positions.append(pos)
        rep.fills += 1
        log.info("FILL       %-9s bought $%.2f at %.6g (impact %.2f%%), stop %.6g, target %.6g", s.symbol, first, fill.price, fill.impact_pct, pos.stop_price, pos.first_target)
        self.journal.decision(now, "entry", s.symbol, {"fill": fill, "tranche": 1}, "tranche 1 filled in zone")

    def _maybe_tranche_two(self, pos: Position, s: TokenSnapshot, now: datetime, rep: TickReport) -> None:
        if pos.tranches_filled != 1 or len(pos.tranche_plan) < 2 or pos.first_target_done:
            return
        if now - pos.opened_at < timedelta(minutes=self.cfg.entry.tranche2_confirm_min):
            return
        closes = [c.close for c in s.candles_5m if c.ts >= pos.opened_at]
        zone_low = pos.entry_zone[0]
        if not closes or min(closes) < zone_low or s.price_usd <= pos.avg_entry:
            return  # trade has not proven itself; never add to a losing first half
        pos.check_add_allowed(s.price_usd)
        usd = round(pos.planned_size_usd * pos.tranche_plan[1]["pct"] / 100, 2)
        prices = {p.address: (s.price_usd if p.address == pos.address else p.avg_entry) for p in self.positions if p.is_open}
        usd = min(usd, self.risk.room_left(self.positions, prices), max(0.0, self.risk.max_position_usd() - pos.cost_usd))
        if usd < 1:
            pos.tranches_filled = 2
            return
        try:
            fill = self.broker.buy(now, usd, s.price_usd, s.liquidity_usd, self.cfg.entry.default_slippage_pct, "tranche 2: trade proved itself")
        except ExecutionError as exc:
            self.journal.decision(now, "entry", pos.symbol, {"tranche": 2, "skipped": True}, str(exc))
            return
        pos.fills.append(fill)
        pos.tranches_filled = 2
        rep.fills += 1
        log.info("FILL       %-9s tranche 2 $%.2f at %.6g", pos.symbol, usd, fill.price)
        self.journal.decision(now, "entry", pos.symbol, {"fill": fill, "tranche": 2}, "held zone; tranche 2 filled")

    # ================================================================== exits

    async def _monitor(self, snaps: dict[str, TokenSnapshot], now: datetime, rep: TickReport) -> None:
        for pos in [p for p in self.positions if p.is_open]:
            s = snaps.get(pos.address)
            if s is None:
                log.warning("%s: data UNKNOWN this tick — holding, will retry", pos.symbol)
                continue
            recheck = safety = None
            interval = timedelta(minutes=self.cfg.gatekeeper.recheck_interval_min)
            alert = self._emergency_hint(pos, s, now)
            if pos.last_recheck_at is None or now - pos.last_recheck_at >= interval or alert:
                safety = await self._safe(self.provider.safety(s), None)
                market = await self._safe(self.provider.market_context(), None)
                if safety is not None and market is not None:
                    rec = Record.from_snapshot(s)
                    recheck = self.gatekeeper.check(rec, s, safety, market, now)
                    pos.last_recheck_at = now
                    self.journal.decision(now, "gatekeeper_recheck", pos.symbol, rec.to_dict()["gatekeeper"], recheck.verdict)

            decision = self.exit.evaluate(pos, s, safety, recheck, now)
            if decision.block.action == "HOLD":
                self._maybe_tranche_two(pos, s, now, rep)
                continue
            self._apply_exit(pos, s, decision, now, rep)

    def _emergency_hint(self, pos: Position, s: TokenSnapshot, now: datetime) -> bool:
        recent = [v for t, v in s.liquidity_history if t >= now - timedelta(minutes=self.cfg.exit.emergency_liq_window_min)]
        if recent and known(s.liquidity_usd):
            return (max(recent) - s.liquidity_usd) / max(recent) * 100 >= self.cfg.exit.emergency_liq_drop_pct / 2
        return False

    def _apply_exit(self, pos: Position, s: TokenSnapshot, decision, now: datetime, rep: TickReport) -> None:
        b = decision.block
        tokens = min(decision.tokens_to_sell, pos.tokens)
        if tokens > 0:
            fills = self.broker.sell(now, tokens, s.price_usd, s.liquidity_usd, emergency=decision.emergency, reason=b.trigger or "")
            pos.fills.extend(fills)
        if decision.new_stop is not None and decision.new_stop > pos.stop_price:
            pos.move_stop(decision.new_stop)
        for flag in decision.mark_flags:
            setattr(pos, flag, True)
        rec = pos.record
        rec["exit"] = {"action": b.action, "pct_of_position": b.pct_of_position, "trigger": b.trigger,
                       "priority": b.priority, "evidence": b.evidence, "next": b.next}
        self.journal.decision(now, "exit", pos.symbol, rec["exit"], b.evidence)
        log.info("EXIT       %-9s %-4s %5.1f%% trigger=%s | %s", pos.symbol, b.action, b.pct_of_position, b.trigger, b.evidence[:140])
        rep.exits.append(f"{pos.symbol}:{b.action}:{b.trigger}")
        if b.action == "EXIT" or pos.tokens <= pos.tokens_bought * 1e-9:
            self._close(pos, now, b.trigger, b.priority)

    def _close(self, pos: Position, now: datetime, trigger: str | None, priority: int | None) -> None:
        pos.closed_at = now
        pos.exit_trigger = trigger
        pos.exit_priority = priority
        self.risk.record_close(pos, now)
        row = self.journal.trade(pos, now)
        self.positions.remove(pos)
        self.closed.append(pos)
        log.info("CLOSED     %-9s pnl $%.2f (%.2fR) via %s", pos.symbol, row["pnl_usd"], row["r_multiple"], trigger)

    async def _exit_all(self, snaps: dict[str, TokenSnapshot], now: datetime, rep: TickReport) -> None:
        log.warning("KILL SWITCH: exiting all positions")
        for pos in [p for p in self.positions if p.is_open]:
            s = snaps.get(pos.address)
            if s is None or not known(s.price_usd, s.liquidity_usd):
                log.error("%s: no price — cannot exit this tick, retrying", pos.symbol)
                continue
            from .exit import ExitDecision
            from .models import ExitBlock

            d = ExitDecision(ExitBlock("EXIT", 100, "kill_switch", 0, f"human requested exit-all at {iso(now)}", "Flat; entries stopped"),
                             tokens_to_sell=pos.tokens, emergency=True)
            self._apply_exit(pos, s, d, now, rep)
        if not any(p.is_open for p in self.positions):
            self.risk.clear_exit_all()

    # ================================================================== bookkeeping

    async def _followups(self, now: datetime) -> None:
        due = self.journal.pending_followups(now)
        if not due:
            return
        snaps = await self._safe(self.provider.snapshots(list({r["token"] for r in due}), False), {})
        self.journal.complete_followups({a: s.price_usd for a, s in snaps.items()})

    def _daily_summary(self, now: datetime, prices: dict[str, float]) -> None:
        day = now.strftime("%Y-%m-%d")
        if now.hour != self.cfg.loop.daily_summary_hour_utc or self.last_summary_day == day:
            return
        self.last_summary_day = day
        summary = self.summary(prices)
        self.journal.decision(now, "daily_summary", "*", summary, "daily summary")
        log.info("DAILY SUMMARY %s", json.dumps(summary))

    def summary(self, prices: dict[str, float] | None = None) -> dict:
        prices = prices or {}
        open_pos = [p for p in self.positions if p.is_open]
        return {
            "mode": self.cfg.mode,
            "equity_usd": round(self.risk.equity(self.positions, prices), 2),
            "realized_pnl_usd": round(self.risk.realized_pnl, 2),
            "open_positions": [{"symbol": p.symbol, "cost": round(p.cost_usd, 2), "stop": p.stop_price, "target": p.first_target} for p in open_pos],
            "pending_plans": [p.record.token.symbol for p in self.plans.values()],
            "closed_trades": len(self.closed),
            "entries_stopped": self.risk.entries_stopped(),
            "drawdown_halt": self.risk.drawdown_halted(),
        }

    def _merge_history(self, s: TokenSnapshot, now: datetime) -> None:
        h = self.history.setdefault(s.address, {"holders": [], "liq": [], "bs": []})
        if known(s.holders):
            h["holders"].append((now, float(s.holders)))
        if known(s.liquidity_usd):
            h["liq"].append((now, float(s.liquidity_usd)))
        if known(s.buys_1h, s.sells_1h) and s.sells_1h:
            h["bs"].append((now, s.buys_1h / s.sells_1h))
        cutoff = now - timedelta(minutes=HISTORY_MIN)
        for key, attr in (("holders", "holder_history"), ("liq", "liquidity_history"), ("bs", "buy_sell_history")):
            merged = {t: v for t, v in getattr(s, attr) + h[key] if t >= cutoff}
            series = sorted(merged.items())
            h[key] = [(t, v) for t, v in h[key] if t >= cutoff]
            setattr(s, attr, series)

    @staticmethod
    def _due(last: datetime | None, minutes: float, now: datetime) -> bool:
        return last is None or now - last >= timedelta(minutes=minutes) - timedelta(seconds=1)

    async def _safe(self, coro, default):
        try:
            return await coro
        except Exception as exc:  # a data source failure must never stop exits
            log.exception("data call failed: %s", exc)
            return default

    # ================================================================== persistence

    def save_state(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "risk": self.risk.to_dict(),
            "positions": [_pos_to_json(p) for p in self.positions],
            "closed_count": len(self.closed),
            "rejected": {a: t.isoformat() for a, t in self.rejected.items()},
        }
        tmp = self.state_dir / "state.json.tmp"
        tmp.write_text(json.dumps(data, default=str, indent=1))
        tmp.replace(self.state_dir / "state.json")

    def load_state(self) -> None:
        path = self.state_dir / "state.json"
        if not path.exists():
            return
        data = json.loads(path.read_text())
        self.risk.load_dict(data.get("risk", {}))
        self.positions = [_pos_from_json(d) for d in data.get("positions", [])]
        self.rejected = {a: datetime.fromisoformat(t) for a, t in data.get("rejected", {}).items()}


def _pos_to_json(p: Position) -> dict:
    d = p.to_dict()
    for k in ("opened_at", "last_recheck_at", "closed_at"):
        d[k] = d[k].isoformat() if d[k] else None
    for f in d["fills"]:
        f["ts"] = f["ts"].isoformat()
    return d


def _pos_from_json(d: dict) -> Position:
    d = dict(d)
    for k in ("opened_at", "last_recheck_at", "closed_at"):
        d[k] = datetime.fromisoformat(d[k]) if d.get(k) else None
    d["fills"] = [Fill(**{**f, "ts": datetime.fromisoformat(f["ts"])}) for f in d["fills"]]
    return Position(**d)


def _score_of(result) -> int | None:
    for d in result.dropped:
        if d.startswith("potential score "):
            return int(d.split()[2])
    return None
