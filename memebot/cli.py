"""Command line.

  memebot run [--sim] [--ticks N]   paper-trading loop (live data, or the simulated market)
  memebot stop                      kill switch 1: stop all NEW entries
  memebot exit-all                  kill switch 2: exit every position (also stops entries)
  memebot resume [--reviewed]       allow entries again (--reviewed also clears a drawdown halt)
  memebot review                    weekly review from the journal
  memebot status                    open positions and switches
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from .broker import LIVE_PHRASE, ExecutionError, make_broker
from .config import load_config
from .journal import Journal
from .portfolio import RiskManager


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="memebot", description="Meme coin trading bot system (paper mode by default)")
    ap.add_argument("--config", default=None, help="path to config.yaml")
    ap.add_argument("--dir", default=".", help="base directory for state/ and journal/")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--sim", action="store_true", help="use the deterministic simulated market (no network)")
    run.add_argument("--ticks", type=int, default=None, help="stop after N one-minute ticks")
    run.add_argument("-v", "--verbose", action="store_true")
    sub.add_parser("stop")
    sub.add_parser("exit-all")
    res = sub.add_parser("resume")
    res.add_argument("--reviewed", action="store_true", help="I have reviewed the journal after a drawdown halt")
    rv = sub.add_parser("review")
    rv.add_argument("--days", type=int, default=7)
    sub.add_parser("status")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    base = Path(args.dir)
    risk = RiskManager(cfg, base / cfg.loop.state_dir)

    if args.cmd == "stop":
        risk.stop_entries()
        print("Kill switch 1 set: no new entries. Exits keep running. Undo with: memebot resume")
        return 0
    if args.cmd == "exit-all":
        risk.request_exit_all()
        print("Kill switch 2 set: the running loop will exit ALL positions on its next tick and stop entries.")
        return 0
    if args.cmd == "resume":
        if risk.drawdown_halted() and not args.reviewed:
            print("Drawdown breaker is active. Review the journal first (memebot review), then: memebot resume --reviewed")
            return 1
        if args.reviewed:
            risk.clear_drawdown_halt()
        risk.resume_entries()
        print("Entries allowed again.")
        return 0
    if args.cmd == "review":
        report = Journal(cfg, base / cfg.loop.journal_dir).weekly_review(days=args.days)
        print(json.dumps(report, indent=2))
        return 0
    if args.cmd == "status":
        state = base / cfg.loop.state_dir / "state.json"
        data = json.loads(state.read_text()) if state.exists() else {}
        print(json.dumps({
            "mode": cfg.mode,
            "entries_stopped": risk.entries_stopped(),
            "exit_all_pending": risk.exit_all_requested(),
            "drawdown_halt": risk.drawdown_halted(),
            "open_positions": [{"symbol": p["symbol"], "stop": p["stop_price"], "target": p["first_target"]} for p in data.get("positions", [])],
            "realized_pnl_usd": data.get("risk", {}).get("realized_pnl", 0.0),
        }, indent=2))
        return 0

    # ---- run
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.DEBUG if args.verbose else logging.WARNING)
    confirmation = None
    if cfg.mode == "live":
        confirmation = input(f"Config says live mode. Type '{LIVE_PHRASE}' to continue: ").strip()
    try:
        broker = make_broker(cfg, confirmation)
    except ExecutionError as exc:
        print(f"Refusing to start: {exc}")
        return 2

    from .loop import Orchestrator

    if args.sim:
        from .data.sim import SimulatedMarket

        provider = SimulatedMarket()
    else:
        from .data.solana_free import SolanaFreeProvider

        provider = SolanaFreeProvider(cfg)
    orch = Orchestrator(cfg, provider, base, broker=broker)
    print(f"memebot: {cfg.mode.upper()} mode, data={provider.name}, capital=${cfg.capital_usd}. Ctrl-C to stop (positions are kept in state/).")
    try:
        asyncio.run(orch.run(ticks=args.ticks, simulated=args.sim))
    except KeyboardInterrupt:
        print("stopped")
    print(json.dumps(orch.summary(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
