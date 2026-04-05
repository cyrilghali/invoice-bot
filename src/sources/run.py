"""
CLI entry point for running a source externally (systemd timer / cron).

Usage:
    python -m sources.run <instance_name>

Loads config.yaml, finds the named source instance, imports its module,
and calls run(). Exits when done.
"""

import os
import sys

import db
from utils import DEFAULT_DATA_DIR, load_config, setup_logging


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python -m sources.run <instance_name>", file=sys.stderr)
        sys.exit(1)

    instance_name = sys.argv[1]

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

    # Pass through top-level config that sources may need
    if "invoices" in config and "invoices" not in instance_config:
        instance_config["invoices"] = config["invoices"]
    if "classifier" in config and "classifier" not in instance_config:
        instance_config["classifier"] = config["classifier"]

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
