"""Trade journal and weekly review. Every decision is logged with timestamp,
inputs, output and reason. The weekly review is how the system gets smarter."""
from __future__ import annotations

import json
import random
from collections import Counter, defaultdict
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import Section
from .models import iso


def _default(o: Any) -> Any:
    if isinstance(o, datetime):
        return iso(o)
    if is_dataclass(o):
        return asdict(o)
    if isinstance(o, float) and o == float("inf"):
        return "inf"
    return str(o)


class Journal:
    def __init__(self, cfg: Section, directory: Path, seed: int | None = None):
        self.cfg = cfg
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.decisions = self.dir / "decisions.jsonl"
        self.trades = self.dir / "trades.jsonl"
        self.rejections = self.dir / "rejections.jsonl"
        self.rule_breaks = self.dir / "rule_breaks.jsonl"
        self._rng = random.Random(seed)

    @staticmethod
    def _append(path: Path, row: dict) -> None:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=_default) + "\n")

    @staticmethod
    def _read(path: Path) -> list[dict]:
        if not path.exists():
            return []
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    # ------------------------------------------------------------------ logging

    def decision(self, now: datetime, bot: str, token: str, output: Any, reason: str, inputs: Any = None) -> None:
        self._append(self.decisions, {"ts": iso(now), "bot": bot, "token": token, "inputs": inputs, "output": output, "reason": reason})

    def rule_break(self, now: datetime, rule: str, detail: str) -> None:
        self._append(self.rule_breaks, {"ts": iso(now), "rule": rule, "detail": detail})

    def maybe_sample_rejection(self, now: datetime, token: str, symbol: str, stage: str, price: Any, reasons: list[str]) -> bool:
        if self._rng.random() > self.cfg.journal.rejection_sample_rate:
            return False
        due = now + timedelta(hours=self.cfg.journal.rejection_followup_hours)
        self._append(self.rejections, {
            "ts": iso(now), "token": token, "symbol": symbol, "stage": stage, "price_at_rejection": price,
            "reasons": reasons, "followup_due": iso(due), "price_after": None,
        })
        return True

    def pending_followups(self, now: datetime) -> list[dict]:
        return [r for r in self._read(self.rejections) if r["price_after"] is None and r["followup_due"] <= iso(now)]

    def complete_followups(self, prices: dict[str, Any]) -> None:
        rows = self._read(self.rejections)
        for r in rows:
            if r["price_after"] is None and r["token"] in prices:
                r["price_after"] = prices[r["token"]]
        with open(self.rejections, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, default=_default) + "\n")

    def trade(self, pos: Any, now: datetime) -> dict:
        fills = pos.fills
        buys = [f for f in fills if f.side == "buy"]
        sells = [f for f in fills if f.side == "sell"]
        row = {
            "ts": iso(now),
            "token": pos.address,
            "symbol": pos.symbol,
            "opened_at": iso(pos.opened_at),
            "closed_at": iso(pos.closed_at),
            "tier": pos.tier,
            "setup": pos.setup,
            "record": pos.record,
            "scout_factors": pos.scout_factors,
            "fills": [asdict(f) for f in fills],
            "fees_usd": round(sum(f.fee_usd for f in fills), 4),
            "planned_slippage_pct": buys[0].planned_slippage_pct if buys else None,
            "actual_buy_impact_pct": round(max((f.impact_pct for f in buys), default=0), 3),
            "actual_sell_impact_pct": round(max((f.impact_pct for f in sells), default=0), 3),
            "cost_usd": round(pos.cost_usd, 4),
            "proceeds_usd": round(pos.proceeds_usd, 4),
            "pnl_usd": round(pos.realized_pnl(), 4),
            "planned_risk_usd": round(pos.planned_risk_usd, 4),
            "r_multiple": round(pos.r_multiple(), 3),
            "exit_trigger": pos.exit_trigger,
            "exit_priority": pos.exit_priority,
        }
        self._append(self.trades, row)
        return row

    # ------------------------------------------------------------------ review

    def weekly_review(self, now: datetime | None = None, days: int = 7) -> dict:
        trades = self._read(self.trades)
        if now is not None:
            cutoff = iso(now - timedelta(days=days))
            trades = [t for t in trades if t["ts"] >= cutoff]
        return review_trades(trades, self._read(self.rejections), self._read(self.rule_breaks), self.cfg)


def review_trades(trades: list[dict], rejections: list[dict], rule_breaks: list[dict], cfg: Section) -> dict:
    n = len(trades)
    wins = [t for t in trades if t["pnl_usd"] > 0]
    losses = [t for t in trades if t["pnl_usd"] <= 0]
    win_rate = len(wins) / n if n else 0.0
    avg_win = sum(t["r_multiple"] for t in wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(t["r_multiple"] for t in losses) / len(losses)) if losses else 0.0
    expectancy = win_rate * avg_win - (1 - win_rate) * avg_loss

    equity, peak, max_dd = 0.0, 0.0, 0.0
    for t in sorted(trades, key=lambda t: t["closed_at"]):
        equity += t["pnl_usd"]
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    triggers = Counter(t["exit_trigger"] for t in trades)
    factor_avgs: dict[str, dict[str, float]] = {}
    for label, group in (("winners", wins), ("losers", losses)):
        sums: dict[str, float] = defaultdict(float)
        for t in group:
            for k, v in (t.get("scout_factors") or {}).items():
                sums[k] += v
        factor_avgs[label] = {k: round(v / len(group), 1) for k, v in sums.items()} if group else {}

    passed_rugs = [
        {"symbol": t["symbol"], "token": t["token"], "evidence": (t.get("record") or {}).get("exit", {}).get("evidence")}
        for t in trades
        if t["exit_trigger"] == "emergency" and t["r_multiple"] <= -1
    ]

    followed = [r for r in rejections if r.get("price_after") not in (None, "UNKNOWN") and r.get("price_at_rejection") not in (None, "UNKNOWN")]
    missed = [r for r in followed if r["price_after"] >= 2 * r["price_at_rejection"]]
    dodged = [r for r in followed if r["price_after"] <= 0.5 * r["price_at_rejection"]]

    report = {
        "trades": n,
        "win_rate": round(win_rate, 3),
        "avg_win_r": round(avg_win, 2),
        "avg_loss_r": round(avg_loss, 2),
        "expectancy_r": round(expectancy, 3),
        "net_pnl_usd": round(sum(t["pnl_usd"] for t in trades), 2),
        "fees_usd": round(sum(t["fees_usd"] for t in trades), 2),
        "max_drawdown_usd": round(max_dd, 2),
        "exits_by_trigger": {k: round(v / n, 3) for k, v in triggers.items()} if n else {},
        "scout_factors_winners_vs_losers": factor_avgs,
        "rugs_or_honeypots_that_passed": passed_rugs,
        "rejections_followed_up": len(followed),
        "rejections_that_doubled": len(missed),
        "rejections_that_halved": len(dodged),
        "rule_breaks": len(rule_breaks),
        "live_ready": n >= cfg.review.min_paper_trades_for_live and expectancy > 0,
    }
    report["suggestion"] = suggest_one_change(report, cfg)
    return report


def suggest_one_change(r: dict, cfg: Section) -> str:
    """One threshold at a time, judged after 20+ new trades."""
    if r["trades"] < cfg.review.min_trades_per_change:
        return f"Not enough trades ({r['trades']}) — change nothing until {cfg.review.min_trades_per_change}+."
    if r["rugs_or_honeypots_that_passed"]:
        return "A rug/honeypot got a PASS. Turn its pattern into a new hard-fail check before touching any threshold."
    exits = r["exits_by_trigger"]
    if exits.get("time_stop", 0) > 0.3:
        return "Over 30% of exits are time stops: raise scout.watchlist_score by 5 (entries lack momentum)."
    if exits.get("invalidation", 0) > 0.5:
        return "Over half of exits are invalidations: raise entry.min_reward_to_risk for tier B to 2.5."
    if r["rejections_followed_up"] >= 20 and r["rejections_that_doubled"] / r["rejections_followed_up"] > 0.3:
        return "Many rejected tokens doubled: filters may be too strict — lower scout.min_holders by 50 and re-test."
    if r["expectancy_r"] <= 0:
        return "Expectancy is not positive: lower risk.max_position_pct to 1.0 while you investigate."
    return "Expectancy positive: change nothing this week; keep collecting trades."
