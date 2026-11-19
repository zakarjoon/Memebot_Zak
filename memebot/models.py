"""Core data types and the Shared handoff format.

Every token travels through the pipeline as one ``Record``. Each bot fills in
only its own block. Missing values are the string ``"UNKNOWN"``, never guessed.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Union

UNKNOWN = "UNKNOWN"
Unknown = Literal["UNKNOWN"]

Num = Union[float, int, Unknown]
Flag = Union[bool, Unknown]


def is_unknown(value: Any) -> bool:
    return value is None or value == UNKNOWN


def known(*values: Any) -> bool:
    return not any(is_unknown(v) for v in values)


def iso(dt: datetime | None) -> str:
    if dt is None:
        return UNKNOWN
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- market data


@dataclass
class Candle:
    ts: datetime  # candle open time, UTC
    open: float
    high: float
    low: float
    close: float
    volume_usd: float


@dataclass
class Holder:
    address: str
    pct: float  # share of total supply, 0-100
    excluded: bool = False  # LP pool, burn address, known launchpad / exchange contract
    label: str = ""


@dataclass
class TokenSnapshot:
    """Everything the bots know about one token at one moment.

    Data adapters fill what they can and leave the rest as UNKNOWN.
    """

    chain: str
    address: str
    symbol: str
    name: str
    pair_address: str | Unknown
    data_timestamp: datetime

    price_usd: Num = UNKNOWN
    liquidity_usd: Num = UNKNOWN
    mcap_usd: Num = UNKNOWN
    vol_1h_usd: Num = UNKNOWN
    vol_24h_usd: Num = UNKNOWN
    holders: Num = UNKNOWN
    age_min: Num = UNKNOWN
    price_change_24h_pct: Num = UNKNOWN
    buys_1h: Num = UNKNOWN
    sells_1h: Num = UNKNOWN
    unique_traders_1h: Num = UNKNOWN

    candles_5m: list[Candle] = field(default_factory=list)
    candles_15m: list[Candle] = field(default_factory=list)
    candles_1h: list[Candle] = field(default_factory=list)

    # time series kept by the orchestrator (or backfilled by a provider)
    holder_history: list[tuple[datetime, float]] = field(default_factory=list)
    liquidity_history: list[tuple[datetime, float]] = field(default_factory=list)
    buy_sell_history: list[tuple[datetime, float]] = field(default_factory=list)  # (ts, buys/sells)

    # signals that free public APIs rarely provide; UNKNOWN unless a source fills them
    proven_wallets_holding: Num = UNKNOWN
    proven_wallets_selling: Flag = UNKNOWN
    social_mentions_1h: Num = UNKNOWN
    social_mentions_peak_1h: Num = UNKNOWN
    social_distinct_aged_accounts: Num = UNKNOWN
    social_paid_or_new_only: Flag = UNKNOWN

    sources: list[str] = field(default_factory=list)

    def age_sec(self, now: datetime) -> float:
        return (now - self.data_timestamp).total_seconds()


@dataclass
class SafetyReport:
    """Contract-safety facts for the Gatekeeper. UNKNOWN wherever a source is silent."""

    data_timestamp: datetime
    sell_simulation_ok: Flag = UNKNOWN
    sell_tax_pct: Num = UNKNOWN
    tax_mutable: Flag = UNKNOWN
    mint_authority_active: Flag = UNKNOWN
    freeze_authority_active: Flag = UNKNOWN
    lp_locked_pct: Num = UNKNOWN
    lp_lock_days_remaining: Num = UNKNOWN  # float("inf") when burned
    upgradeable: Flag = UNKNOWN  # Solana: Token-2022 transfer hook / permanent delegate etc.
    top_holders: list[Holder] = field(default_factory=list)
    holder_clusters: list[list[str]] = field(default_factory=list)  # wallets funded from one source
    deployer_address: str | Unknown = UNKNOWN
    deployer_age_days: Num = UNKNOWN
    deployer_rugged_before: Flag = UNKNOWN
    deployer_moving_funds: Flag = UNKNOWN
    impersonation: Flag = UNKNOWN
    insider_supply_pct: Num = UNKNOWN  # first-block / bundled buys still held
    dev_holding_pct: Num = UNKNOWN
    insiders_selling: Flag = UNKNOWN
    copied_website: Flag = UNKNOWN
    bot_followers: Flag = UNKNOWN
    raw_risks: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)


@dataclass
class MarketContext:
    base_coin_trend_24h_pct: Num = UNKNOWN  # SOL 24h change
    meme_sector_volume_trend_pct: Num = UNKNOWN
    hour_utc: int | Unknown = UNKNOWN


# --------------------------------------------------------------------------- handoff blocks


@dataclass
class TokenBlock:
    chain: str
    address: str
    symbol: str
    pair_address: str
    data_timestamp: str


@dataclass
class ScoutBlock:
    potential_score: int
    factor_scores: dict[str, int]
    why_up: str
    who_buys_next: str
    metrics: dict[str, Any]
    sources: list[str]
    queue: Literal["priority", "watchlist"]
    unknowns: list[str] = field(default_factory=list)


@dataclass
class GatekeeperBlock:
    verdict: Literal["PASS", "CAUTION", "REJECT"]
    hard_fails: list[str]
    risk_score: int | Unknown
    tier: str | None
    size_multiplier: float
    top_risks: list[str]
    thesis_killers: list[str]
    unknowns: list[str]
    checked_at: str
    category_scores: dict[str, float] = field(default_factory=dict)
    confidence: Literal["low", "medium", "high"] = "low"


@dataclass
class EntryBlock:
    decision: Literal["ENTER", "NO_ENTRY"]
    setup: str | None
    entry_zone: list[float] | None
    size_usd: float
    tranches: list[dict[str, Any]]
    stop_price: float | None
    invalidation: str | None
    first_target: float | None
    reward_to_risk: float | None
    max_slippage_pct: float
    expires_at: str | None
    reasons: list[str] = field(default_factory=list)
    size_limits: dict[str, float] = field(default_factory=dict)


@dataclass
class ExitBlock:
    action: Literal["HOLD", "TRIM", "EXIT"]
    pct_of_position: float
    trigger: str | None
    priority: int | None
    evidence: str
    next: str


@dataclass
class Record:
    token: TokenBlock
    scout: ScoutBlock | None = None
    gatekeeper: GatekeeperBlock | None = None
    entry: EntryBlock | None = None
    exit: ExitBlock | None = None

    @classmethod
    def from_snapshot(cls, s: TokenSnapshot) -> "Record":
        return cls(
            token=TokenBlock(
                chain=s.chain,
                address=s.address,
                symbol=s.symbol,
                pair_address=s.pair_address if not is_unknown(s.pair_address) else UNKNOWN,
                data_timestamp=iso(s.data_timestamp),
            )
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"token": asdict(self.token)}
        for name in ("scout", "gatekeeper", "entry", "exit"):
            block = getattr(self, name)
            if block is not None:
                out[name] = asdict(block)
        return out


ALLOWED_VERDICTS = {"PASS", "CAUTION", "REJECT"}
ALLOWED_DECISIONS = {"ENTER", "NO_ENTRY"}
ALLOWED_ACTIONS = {"HOLD", "TRIM", "EXIT"}
