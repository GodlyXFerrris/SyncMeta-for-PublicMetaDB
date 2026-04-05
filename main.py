#!/usr/bin/env python3
"""SIMKL → PublicMetaDB list sync tool.

Usage:
    python main.py sync              # One-time sync
    python main.py sync --dry-run    # Preview without changes
    python main.py sync --interval 30  # Repeat every 30 minutes
    python main.py auth              # Authenticate with SIMKL (PIN flow)
"""

import argparse
import logging
import os
import signal
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from src.config import AppConfig, load_config, validate_config
from src.sync_service import SyncService

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
logger = logging.getLogger("simkl_pmdb_sync")

# Graceful shutdown flag
_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    _shutdown = True
    logger.info("Shutdown signal received, finishing current cycle...")


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format=LOG_FORMAT)
    # Quiet noisy libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)


def cmd_auth(config: AppConfig) -> None:
    """Run SIMKL PIN authentication and print the token."""
    if not config.simkl.client_id:
        logger.error("SIMKL_CLIENT_ID is required for authentication")
        sys.exit(1)

    from src.simkl_client import SimklClient

    # Temporarily allow missing access_token for auth flow
    client = SimklClient(config.simkl)
    print("\n=== SIMKL Authentication ===")
    token = client.authenticate_pin()

    print(f"\nAccess token: {token}")
    print("\nAdd this to your .env file:")
    print(f"  SIMKL_ACCESS_TOKEN={token}")
    print()


def cmd_sync(config: AppConfig, args: argparse.Namespace) -> None:
    """Run the sync (once or on interval)."""
    # CLI flags override config
    if args.dry_run:
        config.sync.dry_run = True
    if args.remove_missing:
        config.sync.remove_missing = True
    if args.interval is not None:
        config.sync.interval_minutes = args.interval

    # Determine which sources are active
    sources = []
    if not args.source or "simkl" in args.source:
        sources.append("simkl")
    if args.source and "anilist" in args.source:
        config.anilist.enabled = True
    if config.anilist.enabled:
        sources.append("anilist")

    errors = validate_config(config, sources)
    if errors:
        for e in errors:
            logger.error("Config error: %s", e)
        sys.exit(1)

    service = SyncService(config)

    if config.sync.interval_minutes > 0:
        _run_loop(service, config.sync.interval_minutes)
    else:
        _run_once(service)


def _run_once(service: SyncService) -> None:
    """Execute a single sync cycle."""
    try:
        results = service.run()
        _print_summary(results)
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception:
        logger.exception("Sync failed")
        sys.exit(1)


def _run_loop(service: SyncService, interval_minutes: int) -> None:
    """Execute sync in a loop with a sleep interval."""
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    logger.info("Running sync every %d minutes (Ctrl+C to stop)", interval_minutes)
    interval_seconds = interval_minutes * 60

    while not _shutdown:
        try:
            results = service.run()
            _print_summary(results)
        except Exception:
            logger.exception("Sync cycle failed, will retry next interval")

        if _shutdown:
            break

        logger.info("Next sync in %d minutes...", interval_minutes)
        # Sleep in small increments to respond to signals promptly
        deadline = time.time() + interval_seconds
        while time.time() < deadline and not _shutdown:
            time.sleep(min(5, deadline - time.time()))

    logger.info("Shutdown complete")


def _print_summary(results: list) -> None:
    print("\n=== Sync Summary ===")
    for stats in results:
        error_count = len(stats.errors)
        print(
            f"  {stats.list_name}: "
            f"fetched={stats.items_fetched} "
            f"resolved={stats.items_resolved} "
            f"added={stats.items_added} "
            f"removed={stats.items_removed} "
            f"dup={stats.items_skipped_duplicate} "
            f"unresolved={stats.items_skipped_unresolved} "
            f"errors={error_count}"
        )
    print()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="simkl-pmdb-sync",
        description="One-way sync of SIMKL watchlists to PublicMetaDB lists",
    )
    parser.add_argument("--config", "-c", help="Path to JSON config file")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")

    sub = parser.add_subparsers(dest="command", required=True)

    # auth
    sub.add_parser("auth", help="Authenticate with SIMKL via PIN flow")

    # sync
    sync_p = sub.add_parser("sync", help="Run the sync")
    sync_p.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
    sync_p.add_argument("--remove-missing", action="store_true",
                        help="Remove items from PMDB that are no longer in SIMKL")
    sync_p.add_argument("--interval", type=int, default=None,
                        help="Repeat sync every N minutes (0 = once)")
    sync_p.add_argument("--source", nargs="+", choices=["simkl", "anilist"],
                        help="Sources to sync (default: simkl + anilist if enabled)")

    return parser


def main() -> None:
    # Load .env from project root
    env_path = Path(__file__).resolve().parent / ".env"
    load_dotenv(env_path)

    parser = build_parser()
    args = parser.parse_args()
    setup_logging(args.verbose)

    config = load_config(args.config)

    if args.command == "auth":
        cmd_auth(config)
    elif args.command == "sync":
        cmd_sync(config, args)


if __name__ == "__main__":
    main()
