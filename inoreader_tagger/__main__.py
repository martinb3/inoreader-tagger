"""Entry point: `python -m inoreader_tagger <serve|run>`."""

import argparse
import sys

from . import cli


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .config import load_settings

    settings = load_settings()
    uvicorn.run(
        "inoreader_tagger.web:create_app",
        factory=True,
        host=args.host or settings.listen_host,
        port=args.port or settings.listen_port,
        log_level=settings.log_level.lower(),
        # One worker only: the scheduler and the SQLite database both assume a
        # single writer in a single process.
        workers=1,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m inoreader_tagger",
        description="Inoreader tagging — scheduled multi-account service, or a one-shot CLI run.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser("serve", help="Run the scheduled service and status page")
    serve_parser.add_argument("--host", default=None, help="Bind address (default: LISTEN_HOST)")
    serve_parser.add_argument("--port", type=int, default=None, help="Bind port (default: LISTEN_PORT)")
    serve_parser.set_defaults(func=_serve)

    run_parser = subparsers.add_parser("run", help="One-shot run for a single account from config.json")
    cli.add_arguments(run_parser)
    run_parser.set_defaults(func=cli.run)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
