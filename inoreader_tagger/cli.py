"""Single-account command line runner.

This is the original config.json workflow, kept for people who just want a cron
entry and no service. It reads credentials and rules from a JSON file and
tracks its high-water mark in a plain text file, exactly as before.

For scheduled multi-account use, run the service instead:
    python -m inoreader_tagger serve
"""

import argparse
import json
import logging
import os
import sys
from typing import Optional

from .api import InoreaderAPI, InoreaderAuthError, InoreaderError
from .db import STATUS_AUTH_REQUIRED, STATUS_SUCCESS
from .tagger import RunParameters, TaggerEngine

logger = logging.getLogger(__name__)

DEFAULT_TIMESTAMP_FILE = ".last_processed_timestamp"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default="config.json", help="Configuration file path")
    parser.add_argument("--dry-run", action="store_true", help="Match rules but never apply tags")
    parser.add_argument("--max-articles", type=int, default=200, help="Maximum articles per run")
    parser.add_argument("--batch-size", type=int, default=100, help="Articles fetched per batch")
    parser.add_argument(
        "--force-timestamp-update",
        action="store_true",
        help="Obsolete and ignored; the high-water mark now advances safely on its own",
    )
    parser.add_argument(
        "--no-timestamp-tracking",
        action="store_true",
        help="Ignore the high-water mark and reconsider all unread articles",
    )
    parser.add_argument(
        "--reset-timestamp", action="store_true", help="Clear the high-water mark and exit"
    )
    parser.add_argument(
        "--timestamp-file",
        default=DEFAULT_TIMESTAMP_FILE,
        help=f"Where the high-water mark is stored (default: {DEFAULT_TIMESTAMP_FILE})",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Print the full run log")


def run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    try:
        with open(args.config, "r") as handle:
            config = json.load(handle)
    except FileNotFoundError:
        print(f"Configuration file {args.config!r} not found.", file=sys.stderr)
        print("Copy config.example.json to config.json and fill it in.", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"Error parsing {args.config}: {exc}", file=sys.stderr)
        return 2

    if args.reset_timestamp:
        if os.path.exists(args.timestamp_file):
            os.remove(args.timestamp_file)
            print(f"Cleared {args.timestamp_file}.")
        else:
            print("No high-water mark to clear.")
        return 0

    missing = [key for key in ("app_id", "app_key") if not config.get(key)]
    if missing:
        print(f"Configuration is missing: {', '.join(missing)}", file=sys.stderr)
        return 2

    refresh_token = config.get("refresh_token")
    if not refresh_token:
        print(
            "No refresh_token in the config. This CLI cannot complete an OAuth\n"
            "flow on its own — run the service (python -m inoreader_tagger serve),\n"
            "connect the account there, or paste an existing refresh token in.",
            file=sys.stderr,
        )
        return 2

    if args.force_timestamp_update:
        print(
            "Note: --force-timestamp-update no longer does anything. Articles are "
            "processed oldest-first, so the high-water mark advances on its own "
            "without risking skipped articles.",
            file=sys.stderr,
        )

    since = None
    if not args.no_timestamp_tracking:
        since = _load_timestamp(args.timestamp_file)

    api = InoreaderAPI(
        app_id=config["app_id"],
        app_key=config["app_key"],
        refresh_token=refresh_token,
        redirect_uri=config.get("redirect_uri", "http://localhost"),
    )

    params = RunParameters(
        rules=config.get("tagging_rules", []),
        max_articles=args.max_articles,
        batch_size=args.batch_size,
        folder_filter=config.get("folder_filter"),
        dry_run=args.dry_run,
        since_timestamp=since,
        force_timestamp_update=args.force_timestamp_update,
    )

    outcome = TaggerEngine(api, params).run()
    print(outcome.log)

    if outcome.status == STATUS_AUTH_REQUIRED:
        print(
            "\nThe stored refresh token is no longer accepted. Re-authorize the "
            "account and update config.json.",
            file=sys.stderr,
        )
        return 3

    if outcome.new_timestamp and not args.no_timestamp_tracking:
        _save_timestamp(args.timestamp_file, outcome.new_timestamp)

    # Inoreader may rotate the refresh token; persist it or the next run fails.
    if api.refresh_token and api.refresh_token != refresh_token:
        config["refresh_token"] = api.refresh_token
        _write_config(args.config, config)
        print("Stored a rotated refresh token in the config file.")

    print(
        f"\nprocessed={outcome.processed} tagged={outcome.tagged} "
        f"skipped={outcome.skipped} errors={outcome.errors}"
    )
    return 0 if outcome.status == STATUS_SUCCESS else 1


def _load_timestamp(path: str) -> Optional[str]:
    try:
        if os.path.exists(path):
            value = open(path).read().strip()
            return value or None
    except OSError as exc:
        logger.warning("Could not read %s: %s", path, exc)
    return None


def _save_timestamp(path: str, value: str) -> None:
    try:
        with open(path, "w") as handle:
            handle.write(value)
    except OSError as exc:
        logger.warning("Could not write %s: %s", path, exc)


def _write_config(path: str, config: dict) -> None:
    try:
        with open(path, "w") as handle:
            json.dump(config, handle, indent=2)
    except OSError as exc:
        logger.warning("Could not update %s: %s", path, exc)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="inoreader-tagger",
        description="Tag Inoreader articles based on URL patterns (single account).",
    )
    add_arguments(parser)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
