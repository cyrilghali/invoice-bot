"""
CLI entry point for running a source externally (systemd timer / cron).

Usage:
    python -m sources.run <instance_name>
    python -m sources.run <instance_name> --file /path/to/file.pdf [--file ...] [--sender "Dad"]

Loads config.yaml, finds the named source instance, imports its module,
and calls run(). Exits when done.

The --file and --sender flags are injected into the source config as
_files and _sender, for sources that accept external input (e.g. manual_source).
"""

import argparse
import os
import sys

import db
from utils import DEFAULT_DATA_DIR, load_config, setup_logging


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run an invoice-bot source externally.",
        usage="python -m sources.run <instance_name> [--file PATH ...] [--sender NAME]",
    )
    parser.add_argument("instance_name", help="Source instance name from config.yaml")
    parser.add_argument(
        "--file", dest="files", action="append", default=[],
        help="File path to process (can be repeated). Used by manual_source.",
    )
    parser.add_argument(
        "--sender", default=None,
        help="Sender label for manual files (default: from config or 'manual').",
    )
    args = parser.parse_args()

    instance_name = args.instance_name
    data_dir = os.environ.get("DATA_DIR", DEFAULT_DATA_DIR)
    os.makedirs(data_dir, exist_ok=True)

    config = load_config()
    log_level = config.get("logging", {}).get("log_level", "INFO")
    setup_logging(data_dir=data_dir, log_level=log_level)

    db.init_db(data_dir)

    sources_config = config.get("sources", {})
    if instance_name not in sources_config:
        print(f"Source '{instance_name}' not found in config.yaml sources", file=sys.stderr)
        sys.exit(1)

    instance_config = sources_config[instance_name]
    module_name = instance_config.get("module", instance_name)
    instance_config["source_name"] = instance_name

    # Inject CLI args for sources that use them (manual_source)
    if args.files:
        instance_config["_files"] = args.files
    if args.sender:
        instance_config["_sender"] = args.sender

    # Pass through top-level config that sources may need
    if "invoices" in config and "invoices" not in instance_config:
        instance_config["invoices"] = config["invoices"]
    if "classifier" in config and "classifier" not in instance_config:
        instance_config["classifier"] = config["classifier"]

    # Inject client_id from microsoft config or AZURE_CLIENT_ID env var
    if "client_id" not in instance_config:
        ms_config = config.get("microsoft", {})
        client_id = ms_config.get("client_id") or os.environ.get("AZURE_CLIENT_ID")
        if client_id:
            instance_config["client_id"] = client_id

    try:
        import importlib
        mod = importlib.import_module(f"sources.{module_name}")
    except ImportError as e:
        print(f"Could not import source module 'sources.{module_name}': {e}", file=sys.stderr)
        sys.exit(1)

    run_fn = getattr(mod, "run", None)
    if run_fn is None:
        print(f"Source module 'sources.{module_name}' has no run() function", file=sys.stderr)
        sys.exit(1)

    run_fn(instance_config, data_dir)


if __name__ == "__main__":
    main()
