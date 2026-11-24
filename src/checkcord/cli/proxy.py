import argparse
import asyncio
import os
import sys

import structlog

from checkcord.core.logging import setup_logging
from checkcord.proxy.config import ProxySettings, write_config

logger = structlog.get_logger(__name__)


def print_mode_guidance(settings: ProxySettings) -> None:
    """Print setup guidance for the proxy_mode the server started in."""
    proto = "https" if settings.use_tls else "http"
    base_url = f"{proto}://{settings.host}:{settings.port}"

    if settings.proxy_mode == "base_url":
        print(
            "Proxy running in base_url mode. Point your IDE/agent at this proxy "
            "directly -- no system-wide root CA trust needed:\n"
            f"  export ANTHROPIC_BASE_URL={base_url}\n"
            f"  export OPENAI_BASE_URL={base_url}\n"
            "Note: some coding-agent CLIs/IDEs don't honor these env vars at all "
            "(hardcoded endpoints) -- use mitm mode for those instead."
        )
    else:
        print(
            "Proxy running in mitm mode. Trust the generated root CA for "
            f"system-wide interception: {settings.ca_path}"
        )


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("proxy", help="Local interception proxy for coding agents")
    proxy_sub = parser.add_subparsers(dest="proxy_command", metavar="COMMAND", required=True)

    start_parser = proxy_sub.add_parser("start", help="Start the proxy server")
    start_parser.add_argument("--port", type=int, help="Port to listen on")
    start_parser.add_argument("--host", type=str, help="Host to bind to")
    start_parser.add_argument(
        "--no-tls", action="store_true", help="Disable TLS (run HTTP instead of HTTPS)"
    )
    start_parser.add_argument(
        "--bypass",
        action="store_true",
        help="Start proxy in bypass mode (pass requests through untouched)",
    )
    proxy_sub.add_parser("on", help="Enable proxy processing (turn bypass off)")
    proxy_sub.add_parser("off", help="Bypass proxy processing (turn bypass on)")
    proxy_sub.add_parser("metrics", help="Display token usage metrics and ROI dashboard")
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> None:
    setup_logging(log_level="INFO", json_format=False)

    if args.proxy_command == "start":
        if args.port:
            os.environ["CHECKCORD_PROXY_PORT"] = str(args.port)
        if args.host:
            os.environ["CHECKCORD_PROXY_HOST"] = args.host
        if args.no_tls:
            os.environ["CHECKCORD_PROXY_USE_TLS"] = "false"
        if args.bypass:
            os.environ["CHECKCORD_BYPASS"] = "true"

        from checkcord.proxy.server import LocalProxyServer

        server = LocalProxyServer()
        loop = asyncio.get_event_loop()

        try:
            loop.run_until_complete(server.start())
            print_mode_guidance(server.settings)
            loop.run_forever()
        except KeyboardInterrupt:
            logger.info("received_keyboard_interrupt_shutting_down")
            loop.run_until_complete(server.stop())
        except Exception as e:
            logger.error("proxy_server_crashed", error=str(e), exc_info=True)
            sys.exit(1)

    elif args.proxy_command == "on":
        try:
            write_config(bypass=False)
            print("Proxy on.")
        except Exception as e:
            print(f"Could not turn the proxy on: {e}", file=sys.stderr)
            sys.exit(1)

    elif args.proxy_command == "off":
        try:
            write_config(bypass=True)
            print("Proxy off.")
        except Exception as e:
            print(f"Could not turn the proxy off: {e}", file=sys.stderr)
            sys.exit(1)

    elif args.proxy_command == "metrics":
        try:
            from checkcord.proxy.telemetry import calculate_cost_and_savings
            from checkcord.storage.sqlite import SQLiteTelemetryStore

            db_path = ".context/telemetry.db"
            if not os.path.exists(db_path):
                print(
                    "No telemetry logs found. Start the proxy and make requests first.",
                    file=sys.stderr,
                )
                sys.exit(0)

            store = SQLiteTelemetryStore(db_path)

            async def run_metrics() -> None:
                summary = await store.get_summary()
                all_logs = await store.get_all_logs()

                total_requests = summary["total_requests"]
                total_prompt = summary["total_prompt"]
                total_completion = summary["total_completion"]
                total_cache_creation = summary["total_cache_creation"]
                total_cache_read = summary["total_cache_read"]

                total_cost = 0.0
                total_savings = 0.0

                for log in all_logs:
                    cost, savings = calculate_cost_and_savings(
                        provider=log["provider"],
                        model=log["model"],
                        prompt_tokens=log["prompt_tokens"],
                        completion_tokens=log["completion_tokens"],
                        cache_creation_input_tokens=log["cache_creation_input_tokens"],
                        cache_read_input_tokens=log["cache_read_input_tokens"],
                    )
                    total_cost += cost
                    total_savings += savings

                cache_hit_rate = (
                    (total_cache_read / total_prompt * 100.0) if total_prompt > 0 else 0.0
                )

                print("=" * 50)
                print("Checkcord Telemetry Dashboard".center(50))
                print("=" * 50)
                print("  Metric                  | Value")
                print("-" * 26 + "+" + "-" * 23)
                print(f"  Total Requests          | {total_requests}")
                print(f"  Cache Hit Rate          | {cache_hit_rate:.1f} %")
                print(f"  Estimated Actual Cost   | $ {total_cost:.4f}")
                print(f"  Estimated Cost Saved    | $ {total_savings:.4f}")
                print("-" * 26 + "+" + "-" * 23)
                print(f"  Prompt Tokens           | {total_prompt:,}")
                print(f"  - Cache Read (Hit)      | {total_cache_read:,}")
                print(f"  - Cache Creation (Write)| {total_cache_creation:,}")
                standard_input = max(0, total_prompt - total_cache_read - total_cache_creation)
                print(f"  - Standard Input        | {standard_input:,}")
                print(f"  Completion Tokens       | {total_completion:,}")
                print("=" * 50)

            asyncio.run(run_metrics())
        except Exception as e:
            print(f"Could not show metrics: {e}", file=sys.stderr)
            sys.exit(1)
