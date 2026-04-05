"""
Invoice Bot - Main entry point.

Schedules source jobs dynamically from config.yaml and a monthly Excel report.
Sources with `interval_minutes` are scheduled by APScheduler.
Sources without it are triggered externally (systemd timer / cron).

All configuration is read from config.yaml (or CONFIG_PATH env var).
"""

import fcntl
import importlib
import logging
import os
import sys
from datetime import datetime, timezone

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

import db
from excel_exporter import build_monthly_excel
from onedrive_uploader import upload_attachment
from utils import DEFAULT_DATA_DIR, load_config, setup_logging

logger = logging.getLogger("main")


# ---------------------------------------------------------------------------
# Source discovery and scheduling
# ---------------------------------------------------------------------------

def _load_source_module(module_name: str):
    """Import a source module from src/sources/ and return its run function."""
    try:
        mod = importlib.import_module(f"sources.{module_name}")
    except ImportError as e:
        logger.warning("Source module 'sources.%s' not found: %s — skipping", module_name, e)
        return None

    run_fn = getattr(mod, "run", None)
    if run_fn is None:
        logger.warning("Source module 'sources.%s' has no run() function — skipping", module_name)
        return None

    return run_fn


def _build_instance_config(config: dict, instance_name: str) -> dict | None:
    """Build a merged instance config from the top-level config for a given source instance."""
    sources_config = config.get("sources", {})
    instance_config = sources_config.get(instance_name)
    if instance_config is None:
        return None

    # Make a copy to avoid mutating the parsed config
    instance_config = dict(instance_config)
    instance_config["source_name"] = instance_name

    # Inherit top-level config that sources may need
    if "client_id" not in instance_config:
        instance_config["client_id"] = config.get("microsoft", {}).get("client_id")
    if "invoices" in config and "invoices" not in instance_config:
        instance_config["invoices"] = config["invoices"]
    if "classifier" in config and "classifier" not in instance_config:
        instance_config["classifier"] = config["classifier"]
    if "debug" in config:
        if "since_date" not in instance_config and config["debug"].get("since_date"):
            instance_config["since_date"] = config["debug"]["since_date"]

    return instance_config


def _make_source_runner(run_fn, instance_name: str, data_dir: str):
    """Return a callable that APScheduler can invoke for a source job.

    Config is reloaded from disk on every invocation so changes take effect
    without restarting the process.
    """
    def runner():
        try:
            config = load_config(exit_on_error=False)
            instance_config = _build_instance_config(config, instance_name)
            if instance_config is None:
                logger.warning("Source '%s' removed from config — skipping run", instance_name)
                return
            run_fn(instance_config, data_dir)
        except Exception as e:
            logger.error("Source %s crashed: %s", instance_name, e, exc_info=True)
    return runner


def _register_sources(scheduler: BlockingScheduler, config: dict, data_dir: str) -> None:
    """Discover and register source jobs from config."""
    sources_config = config.get("sources", {})
    if not sources_config:
        logger.warning("No sources configured in config.yaml")
        return

    for instance_name, instance_config in sources_config.items():
        module_name = instance_config.get("module", instance_name)
        run_fn = _load_source_module(module_name)
        if run_fn is None:
            continue

        interval = instance_config.get("interval_minutes")
        if interval:
            runner = _make_source_runner(run_fn, instance_name, data_dir)
            scheduler.add_job(
                runner,
                trigger=IntervalTrigger(minutes=int(interval)),
                id=f"source_{instance_name}",
                name=f"Source: {instance_name} (every {interval}m)",
                next_run_time=datetime.now(tz=timezone.utc),
            )
            logger.info("Registered source '%s' (module=%s) — every %d min", instance_name, module_name, interval)
        else:
            logger.info("Source '%s' (module=%s) — externally triggered, not scheduled", instance_name, module_name)


# ---------------------------------------------------------------------------
# Monthly Excel report job
# ---------------------------------------------------------------------------

def send_report() -> None:
    """Build a monthly Excel summary and upload it to OneDrive.

    Config is reloaded from disk so changes take effect without restarting.
    """
    config = load_config(exit_on_error=False)
    now = datetime.now(tz=timezone.utc)
    report_year, report_month = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)

    logger.info("========== REPORT START ==========")
    logger.info("Monthly Excel report triggered for %d/%02d", report_year, report_month)

    data_dir = os.environ.get("DATA_DIR", DEFAULT_DATA_DIR)

    if db.has_monthly_report_been_sent(data_dir, report_year, report_month):
        logger.info("Report for %d/%02d already done. Skipping.", report_year, report_month)
        return

    invoices = db.get_unreported_invoices(data_dir, report_year, report_month)

    if not invoices:
        logger.info("No invoices for %d/%02d — skipping Excel.", report_year, report_month)
        db.save_monthly_report(data_dir, report_year, report_month)
        return

    client_id: str = config["microsoft"]["client_id"]
    report_account: str | None = config.get("onedrive", {}).get("account") or None
    root_folder_name: str = config["onedrive"]["folder_name"]
    excel_filename = f"{report_year}-{report_month:02d}_summary.xlsx"

    try:
        excel_bytes = build_monthly_excel(invoices, report_year, report_month)
        _, excel_link = upload_attachment(
            client_id=client_id,
            root_folder_name=root_folder_name,
            attachment_name=excel_filename,
            attachment_bytes=excel_bytes,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            sender="summary",
            received_at=f"{report_year}-{report_month:02d}-01T00:00:00Z",
            year=report_year,
            month=report_month,
            account_hint=report_account,
        )
        logger.info("Excel summary uploaded to OneDrive: %s", excel_link)
    except Exception as e:
        logger.error("Failed to build/upload Excel summary: %s", e, exc_info=True)
        return

    db.mark_invoices_reported(data_dir, [inv["id"] for inv in invoices])
    db.save_monthly_report(data_dir, report_year, report_month)

    logger.info(
        "Report done: %d invoice(s) marked reported for %d/%02d.",
        len(invoices), report_year, report_month,
    )
    logger.info("========== REPORT END ==========\n")


def _acquire_lock(data_dir: str):
    """Acquire an exclusive file lock. Exits if another instance is running."""
    lock_path = os.path.join(data_dir, "bot.lock")
    lock_file = open(lock_path, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(f"Another instance is already running (lock: {lock_path})", file=sys.stderr)
        sys.exit(1)
    lock_file.write(str(os.getpid()))
    lock_file.flush()
    return lock_file  # keep reference alive — lock releases when process dies


def main() -> None:
    # Bootstrap logging before anything else so all startup messages are captured
    data_dir = os.environ.get("DATA_DIR", DEFAULT_DATA_DIR)
    os.makedirs(data_dir, exist_ok=True)
    _lock = _acquire_lock(data_dir)  # noqa: F841 — must stay alive

    config = load_config()
    log_level = config.get("logging", {}).get("log_level", "INFO")
    setup_logging(data_dir=data_dir, log_level=log_level)

    logger.info("Invoice Bot starting up")

    # Initialize DB
    db.init_db(data_dir)

    # Validate required config fields
    _placeholders = {"YOUR_CLIENT_ID_HERE", "YOUR_FOLDER_NAME_HERE", "your-client-id"}
    required = [
        ("microsoft", "client_id"),
        ("onedrive", "folder_name"),
    ]
    for section, key in required:
        value = config.get(section, {}).get(key)
        if not value or value in _placeholders:
            logger.error(
                "Missing or unconfigured value: %s.%s — set it in config.yaml or via environment variable",
                section, key,
            )
            sys.exit(1)

    report_day = config.get("schedule", {}).get("report_day_of_month", 1)
    report_hour = config.get("schedule", {}).get("report_hour", 8)

    scheduler = BlockingScheduler(timezone="UTC")

    # Register sources from config
    _register_sources(scheduler, config, data_dir)

    # Monthly report job
    scheduler.add_job(
        send_report,
        trigger=CronTrigger(day=report_day, hour=report_hour, minute=0),
        id="monthly_report",
        name="Upload monthly Excel summary to OneDrive",
    )

    logger.info(
        "Scheduler started. Monthly Excel on day %d at %02d:00 UTC.",
        report_day, report_hour,
    )

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")


if __name__ == "__main__":
    main()
