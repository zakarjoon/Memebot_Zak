"""Positions and the portfolio-level hard rules that no bot may break."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import Section


class RuleViolation(Exception):
    """Raised when code tries to break a hard rule (e.g. widen a stop)."""


@dataclass
class Fill:
    ts: datetime
    side: str  # buy | sell
    tokens: float
    price: float  # effective price incl. impact
    usd: float  # cash out (buy) or cash in (sell), net of fees
    fee_usd: float
    impact_pct: float
    planned_slippage_pct: float
    reason: str = ""


@dataclass
class Position:
    address: str
    symbol: str
    opened_at: datetime
    tier: str
    size_multiplier: float
    planned_size_usd: float
    stop_price: float
    initial_stop: float
    first_target: float
    setup: str
    tranche_plan: list[dict[str, Any]]
    entry_zone: list[float]
    planned_risk_usd: float
    record: dict[str, Any] = field(default_factory=dict)  # full handoff record at entry
    scout_factors: dict[str, int] = field(default_factory=dict)
    fills: list[Fill] = field(default_factory=list)
    tranches_filled: int = 0
    highest_close: float = 0.0
    first_target_done: bool = False
    double_done: bool = False
    triple_done: bool = False
    decay_trimmed: bool = False
    dev_holding_at_entry: float | str = "UNKNOWN"
    entry_sell_tax_pct: float | str = "UNKNOWN"
    entry_insiders_selling: bool | str = "UNKNOWN"
    last_recheck_at: datetime | None = None
    closed_at: datetime | None = None
    exit_trigger: str | None = None
    exit_priority: int | None = None

    # ---------------------------------------------------------------- accounting

    @property
    def tokens(self) -> float:
        return sum(f.tokens if f.side == "buy" else -f.tokens for f in self.fills)

    @property
    def tokens_bought(self) -> float:
        return sum(f.tokens for f in self.fills if f.side == "buy")

    @property
    def cost_usd(self) -> float:
        return sum(f.usd for f in self.fills if f.side == "buy")

    @property
    def proceeds_usd(self) -> float:
        return sum(f.usd for f in self.fills if f.side == "sell")

    @property
    def avg_entry(self) -> float:
        bought = self.tokens_bought
        return self.cost_usd / bought if bought else 0.0

    @property
    def is_open(self) -> bool:
        return self.closed_at is None

    def market_value(self, price: float) -> float:
        return self.tokens * price

    def unrealized_pnl(self, price: float) -> float:
        return self.proceeds_usd + self.market_value(price) - self.cost_usd

    def realized_pnl(self) -> float:
        """Only meaningful once closed."""
        return self.proceeds_usd - self.cost_usd

    def r_multiple(self) -> float:
        return self.realized_pnl() / self.planned_risk_usd if self.planned_risk_usd else 0.0

    # ---------------------------------------------------------------- hard rules

    def move_stop(self, new_stop: float) -> bool:
        """Stops only move toward profit. Never further away."""
        if new_stop < self.stop_price:
            raise RuleViolation(
                f"{self.symbol}: refusing to move stop away from {self.stop_price:.8g} to {new_stop:.8g}"
            )
        changed = new_stop > self.stop_price
        self.stop_price = new_stop
        return changed

    def check_add_allowed(self, price: float) -> None:
        """Never average down: no adds while the position is losing."""
        if self.tokens > 0 and price <= self.avg_entry:
            raise RuleViolation(f"{self.symbol}: refusing to add to a losing position (avg {self.avg_entry:.8g}, now {price:.8g})")

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


class RiskManager:
    """Portfolio rules: position cap, total exposure, max positions, daily loss,
    drawdown breaker, re-entry cooldown, and the kill switch."""

    STOP_FILE = "STOP_ENTRIES"
    EXIT_ALL_FILE = "EXIT_ALL"
    HALT_FILE = "DRAWDOWN_HALT"

    def __init__(self, cfg: Section, state_dir: Path):
        self.cfg = cfg
        self.r = cfg.risk
        self.capital = float(cfg.capital_usd)
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.realized_pnl = 0.0
        self.peak_equity = self.capital
        self.daily_losses: list[tuple[datetime, float]] = []
        self.paused_until: datetime | None = None
        self.cooldowns: dict[str, datetime] = {}
        self.size_factor = 1.0  # 0.5 for the first live trades

    # ---------------------------------------------------------------- kill switch

    def _flag(self, name: str) -> Path:
        return self.state_dir / name

    def stop_entries(self) -> None:
        self._flag(self.STOP_FILE).touch()

    def resume_entries(self) -> None:
        self._flag(self.STOP_FILE).unlink(missing_ok=True)

    def request_exit_all(self) -> None:
        self._flag(self.EXIT_ALL_FILE).touch()
        self.stop_entries()

    def exit_all_requested(self) -> bool:
        return self._flag(self.EXIT_ALL_FILE).exists()

    def clear_exit_all(self) -> None:
        self._flag(self.EXIT_ALL_FILE).unlink(missing_ok=True)

    def entries_stopped(self) -> bool:
        return self._flag(self.STOP_FILE).exists()

    def drawdown_halted(self) -> bool:
        return self._flag(self.HALT_FILE).exists()

    def clear_drawdown_halt(self) -> None:
        """Only after the human has reviewed the journal."""
        self._flag(self.HALT_FILE).unlink(missing_ok=True)

    # ---------------------------------------------------------------- equity

    def equity(self, positions: list[Position], prices: dict[str, float]) -> float:
        unreal = sum(
            p.unrealized_pnl(prices[p.address]) for p in positions if p.is_open and p.address in prices
        )
        return self.capital + self.realized_pnl + unreal

    def record_close(self, pos: Position, now: datetime) -> None:
        pnl = pos.realized_pnl()
        self.realized_pnl += pnl
        if pnl < 0:
            self.daily_losses.append((now, -pnl))
        self.cooldowns[pos.address] = now + timedelta(hours=self.r.reentry_cooldown_hours)

    def update(self, positions: list[Position], prices: dict[str, float], now: datetime) -> list[str]:
        """Refresh peak, daily-loss and drawdown state. Returns any new alerts."""
        alerts = []
        eq = self.equity(positions, prices)
        self.peak_equity = max(self.peak_equity, eq)

        cutoff = now - timedelta(hours=24)
        self.daily_losses = [(t, x) for t, x in self.daily_losses if t >= cutoff]
        loss_24h = sum(x for _, x in self.daily_losses)
        open_loss = -sum(min(0.0, p.unrealized_pnl(prices[p.address])) for p in positions if p.is_open and p.address in prices)
        limit = self.capital * self.r.daily_loss_limit_pct / 100
        if loss_24h + open_loss >= limit and (self.paused_until is None or self.paused_until < now):
            self.paused_until = now + timedelta(hours=self.r.daily_loss_pause_hours)
            alerts.append(f"Daily loss limit hit (${loss_24h + open_loss:.2f} >= ${limit:.2f}); entries paused until {self.paused_until:%Y-%m-%d %H:%M} UTC")

        dd = (self.peak_equity - eq) / self.peak_equity * 100 if self.peak_equity else 0
        if dd >= self.r.drawdown_breaker_pct and not self.drawdown_halted():
            self._flag(self.HALT_FILE).write_text(f"{now.isoformat()} drawdown {dd:.1f}%\n")
            alerts.append(f"Drawdown breaker: {dd:.1f}% from peak. New entries halted until a human reviews the journal (memebot resume --reviewed).")
        return alerts

    # ---------------------------------------------------------------- limits

    def exposure(self, positions: list[Position], prices: dict[str, float]) -> float:
        total = 0.0
        for p in positions:
            if p.is_open:
                price = prices.get(p.address, p.avg_entry)
                total += max(p.market_value(price), 0.0)
        return total

    def room_left(self, positions: list[Position], prices: dict[str, float]) -> float:
        cap = self.capital * self.r.max_total_exposure_pct / 100
        return max(0.0, cap - self.exposure(positions, prices))

    def max_position_usd(self) -> float:
        return self.capital * self.r.max_position_pct / 100 * self.size_factor

    def entry_blockers(self, address: str, positions: list[Position], prices: dict[str, float], now: datetime) -> list[str]:
        reasons = []
        if self.exit_all_requested():
            reasons.append("kill switch: exit-all in progress")
        if self.entries_stopped():
            reasons.append("kill switch: new entries stopped")
        if self.drawdown_halted():
            reasons.append("drawdown breaker active — human review required")
        if self.paused_until and now < self.paused_until:
            reasons.append(f"daily loss limit — paused until {self.paused_until:%H:%M} UTC")
        open_pos = [p for p in positions if p.is_open]
        if len(open_pos) >= self.r.max_open_positions:
            reasons.append(f"max open positions ({self.r.max_open_positions}) reached")
        if any(p.address == address for p in open_pos):
            reasons.append("already holding this token")
        until = self.cooldowns.get(address)
        if until and now < until:
            reasons.append(f"re-entry cooldown until {until:%H:%M} UTC")
        if self.room_left(positions, prices) <= 0:
            reasons.append("total memecoin exposure cap reached")
        return reasons

    # ---------------------------------------------------------------- persistence

    def to_dict(self) -> dict[str, Any]:
        return {
            "realized_pnl": self.realized_pnl,
            "peak_equity": self.peak_equity,
            "daily_losses": [(t.isoformat(), x) for t, x in self.daily_losses],
            "paused_until": self.paused_until.isoformat() if self.paused_until else None,
            "cooldowns": {a: t.isoformat() for a, t in self.cooldowns.items()},
            "size_factor": self.size_factor,
        }

    def load_dict(self, d: dict[str, Any]) -> None:
        self.realized_pnl = d.get("realized_pnl", 0.0)
        self.peak_equity = d.get("peak_equity", self.capital)
        self.daily_losses = [(datetime.fromisoformat(t), x) for t, x in d.get("daily_losses", [])]
        pu = d.get("paused_until")
        self.paused_until = datetime.fromisoformat(pu) if pu else None
        self.cooldowns = {a: datetime.fromisoformat(t) for a, t in d.get("cooldowns", {}).items()}
        self.size_factor = d.get("size_factor", 1.0)
