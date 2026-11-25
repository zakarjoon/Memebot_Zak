# memebot — Meme coin trading bot system (paper trading)

A Python build of the *Meme Coin Trading Bot System — Master Prompt*: a
four-bot pipeline for Solana memecoins with one gate and two feedback loops.

```
Scout ──top 5──▶ Gatekeeper ──PASS/CAUTION──▶ Entry ──position──▶ Exit ──every decision──▶ Journal
                    │  ▲                                           │                         │
                 REJECT └──────────── re-check every 15 min ───────┘                         │
                    ▼                                                                        │
                 Discard (24h, no overrides)        weekly review tunes thresholds ◀─────────┘
```

**Paper mode is the default and the only implemented mode.** Live order
execution is deliberately not built (see [Going live](#going-live)).

## Quick start

```bash
git clone https://github.com/zakarjoon/Memebot_Zak.git
cd Memebot_Zak
pip install -e ".[dev]"
```

Try it on the deterministic simulated market (no network needed):

```bash
memebot --dir runs/sim run --sim --ticks 700
```

Then read the weekly review:

```bash
memebot --dir runs/sim review
```

Paper-trade against live Solana data (free endpoints, no API keys):

```bash
memebot --dir runs/live run
```

### Commands

| Command | What it does |
|---|---|
| `memebot run [--sim] [--ticks N]` | Run the loop: one tick per minute; Scout scans every 5 min |
| `memebot stop` | **Kill switch 1** — stop all new entries; exits keep running |
| `memebot exit-all` | **Kill switch 2** — the running loop exits every position on its next tick (and stops entries) |
| `memebot resume [--reviewed]` | Allow entries again. After a drawdown halt, `--reviewed` is required |
| `memebot review [--days 7]` | Weekly review: win rate, avg win/loss in R, expectancy, drawdown, exits by trigger, Scout factors in winners vs losers, rugs that passed, one suggested change |
| `memebot status` | Open positions and switch states |

The kill switches are flag files in `state/`, so they work from a second
terminal while the loop runs.

## Layout

```
config.yaml            every threshold (all marked "default" in the spec) — nothing is hard-coded
memebot/
  models.py            Shared handoff format (Record with token/scout/gatekeeper/entry/exit blocks), UNKNOWN
  scout.py             Bot 1 — filters, Potential Score (0-100), copycat drops, "who buys after us?"
  gatekeeper.py        Bot 2 — stage 1 hard fails, stage 2 risk score, tiers A/B/C, thesis killers
  entry.py             Bot 3 — pre-flight, 3 setups, do-not-enter rules, size = min(C·r·m/d, C·0.02·m, L·0.01, R)
  exit.py              Bot 4 — emergency > invalidation > profit ladder > trailing > momentum decay > time stop
  portfolio.py         Positions + portfolio hard rules (caps, daily loss, drawdown breaker, cooldown, kill switch)
  broker.py            Paper broker (pool-impact model, chunked exits, emergency slippage); live gate
  journal.py           JSONL journal (decisions, trades, sampled rejections + 24h follow-up) and weekly review
  loop.py              Orchestrator wiring it all together; state persisted to state/state.json
  data/solana_free.py  DEX Screener, GeckoTerminal, RugCheck, Jupiter adapters (no keys)
  data/sim.py          Deterministic simulated market with traps (rug, honeypot, mint, whale, copycat)
tests/                 85 tests — at least one per hard rule, plus bots, adapters, end-to-end
```

## How the hard rules are enforced

| Rule | Where |
|---|---|
| Never trade a Gatekeeper REJECT | `Entry.plan` returns NO_ENTRY; `Orchestrator._open` raises `RuleViolation`; rejected tokens are discarded for 24h |
| Max 2% per coin, 10% total, 5 positions | `position_size`, `RiskManager.entry_blockers`, re-checked at fill time |
| Daily loss limit 3% → 24h pause | `RiskManager.update` (realized losses + open losses) |
| Drawdown 15% from peak → halt until review | `DRAWDOWN_HALT` flag; `memebot resume --reviewed` |
| Stops only move toward profit | `Position.move_stop` raises on a wider stop |
| Never average down | `Position.check_add_allowed`; tranche 2 only after the trade proves itself |
| No pump-and-dump / wash trading | Scout drops paid/new-account buzz; Gatekeeper scores volume authenticity |
| Paper default; live needs `LIVE MODE ON` | `make_broker`; live broker not implemented |
| Data source fails → UNKNOWN, block entries, exits continue | `Http.get_json` retries with backoff then returns UNKNOWN; every UNKNOWN critical check is a REJECT; the monitor loop keeps running |
| Unknown is risk, never neutral | Unknown Scout factors score 0; unknown Gatekeeper sub-risks score the full weight |

## What to know before trusting it

1. **Free data leaves real gaps.** No free endpoint provides proven-wallet
   activity, social velocity or authenticity, deployer wallet age and fund
   flows, or unique-trader counts. Per the spec these count as risk. In
   practice a token with no wallet or social data tops out around 60–70 Potential
   Score (watchlist at best), and its Gatekeeper risk score carries about 10–15 points
   from unknowns. To fill those fields, add paid sources (Birdeye, Helius, GMGN,
   Bubblemaps, an X/Telegram feed) in `data/solana_free.py`. The fields already
   exist on `TokenSnapshot` / `SafetyReport`.
2. **The live adapters were tested against the real APIs (Nov 2026).**
   Testing found and fixed four problems: a false LP-lock hard fail from
   expired lockers, Jupiter's `priceImpactPct` not reflecting order size (impact
   is now measured against a small reference quote), a bogus RugCheck
   insider network holding more than the whole supply, and GeckoTerminal 429s
   below its documented limit (it is now capped at 10 req/min and waits out the
   window). Free APIs change without notice, so watch the logs for `UNKNOWN`
   warnings. Any field that won't parse fails safe (REJECT).
3. **The Jupiter "sell simulation" is a proxy.** A quote proves a route exists,
   not that a signed sell would succeed. Freeze authority and Token-2022
   transfer hooks / permanent delegates (the main Solana honeypot mechanisms) are
   checked separately via RugCheck.
4. **The simulated market is a test fixture, not evidence of edge.** Its
   price paths are scripted to exercise every rule. Its P&L means nothing.
5. **A stop may not fill.** In the simulation, a rug costs about −14R on a $20
   position: about 1.9% of capital. That is why the 2% position cap, not the
   stop, is the real protection.

## Tuning (from the journal, never from feelings)

- Change **one** threshold in `config.yaml` at a time, and judge it only after
  20+ new trades. `memebot review` suggests at most one change.
- Edit `scout.narratives` weekly. It is your read of the current themes.
- Every rug or honeypot that got a PASS appears in the review. Turn each into a
  new hard-fail check.

## Going live

Not in this build, on purpose. The spec's bar:

- 50+ paper trades with positive expectancy after fees and slippage (`live_ready` in the review),
- a dedicated hot wallet that holds only trading capital, with its key in an environment variable or secrets manager (never in code, logs or chat),
- MEV-protected routing, and
- the first 50 live trades at half size (`RiskManager.size_factor = 0.5`).

Only then should a reviewed live executor be wired in behind `make_broker`,
which already refuses to start unless `mode: live` *and* the human types
`LIVE MODE ON`.

## Tests

```bash
pytest -q
```
