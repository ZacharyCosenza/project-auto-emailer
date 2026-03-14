import argparse
import logging
import sys

from .config import load_config
from .core import run
from .scheduler import start_daemon


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr
    )


def main():
    parser = argparse.ArgumentParser(prog="auto-emailer")
    parser.add_argument("--config", default="config.json", help="config file path")

    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="run once (search + LLM + email)")
    run_parser.add_argument("--dry-run", action="store_true", help="print output instead of emailing")

    subparsers.add_parser("start", help="start scheduler daemon")

    subparsers.add_parser("install", help="install systemd user timer for automatic scheduling")

    usage_parser = subparsers.add_parser("usage", help="show API usage for the current month")
    usage_parser.add_argument("--month", metavar="YYYY-MM", help="month to report (default: current)")

    args = parser.parse_args()
    setup_logging()

    if args.command == "run":
        config = load_config(args.config, require_secrets=not args.dry_run)
        run(config, dry_run=args.dry_run)
    elif args.command == "start":
        config = load_config(args.config, require_secrets=True)
        start_daemon(config)
    elif args.command == "install":
        from .installer import install
        config = load_config(args.config, require_secrets=False)
        install(config, args.config)
    elif args.command == "usage":
        from .usage import print_report
        print_report(getattr(args, "month", None))


if __name__ == "__main__":
    main()
