"""uv run python -m sim up | run [--level easy|hard|all] [--only ID,ID] [--keep] | down | world"""

from __future__ import annotations

import argparse
import asyncio

from sim import harness
from sim.world import Config, serve


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m sim", description="Simulated company tests")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("up", help="start the world and the stack, and set up the company")
    run = sub.add_parser("run", help="run scenarios against the stack")
    run.add_argument("--level", choices=("easy", "hard", "all"), default="easy")
    run.add_argument("--only", help="comma-separated scenario ids")
    run.add_argument("--keep", action="store_true", help="allow a stack that already ran")
    sub.add_parser("down", help="remove the stack and its volumes, stop the world, delete .env")
    sub.add_parser("world", help="serve the stand-in providers in the foreground")
    args = parser.parse_args(argv)
    if args.command == "up":
        asyncio.run(harness.up())
    elif args.command == "run":
        only = [s.strip() for s in args.only.split(",")] if args.only else None
        asyncio.run(harness.run_scenarios(args.level, only, args.keep))
    elif args.command == "down":
        harness.down()
    else:
        serve(Config.from_env())


if __name__ == "__main__":
    main()
