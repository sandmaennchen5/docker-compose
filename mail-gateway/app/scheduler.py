#!/usr/bin/env python3
"""
Scheduler for Mail Gateway.

Periodically:
  1. Runs getmail6 for every configured account (fetches new mail → msmtpq).
  2. Runs msmtp-runqueue to flush the outbound queue to Synology MailPlus.

Status is written to /runtime/status/status.json so the Web UI can read it.

A trigger file /runtime/trigger_fetch can be written by external processes
(e.g. the REST API) to request an immediate, out-of-schedule fetch.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml
from apscheduler.schedulers.background import BackgroundScheduler

# ── Paths ─────────────────────────────────────────────────────────────────────

CONFIG_FILE = Path("/config/config.yaml")
ACCOUNTS_FILE = Path("/config/accounts.yaml")
GETMAIL_DIR = Path("/runtime/getmail")
STATUS_FILE = Path("/runtime/status/status.json")
TRIGGER_FILE = Path("/runtime/trigger_fetch")

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(
            "/logs/scheduler.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        ),
    ],
)

log = logging.getLogger("scheduler")

# ── Shared state (in-process only; persisted to status.json) ─────────────────

_status: dict = {
    "last_fetch": None,
    "last_error": None,
    "smtp_ok": None,
    "queue_size": 0,
    "running": False,
    "fetch_count": 0,
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_status() -> None:
    try:
        STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATUS_FILE.write_text(json.dumps(_status, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("Could not write status file: %s", exc)


def _queue_size() -> int:
    spool = Path("/var/spool/msmtp")
    if not spool.exists():
        return 0
    # msmtpq stores queue files with a .mail suffix
    return len(list(spool.iterdir()))


def _load_config() -> dict:
    with CONFIG_FILE.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _load_accounts() -> list[dict]:
    with ACCOUNTS_FILE.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("accounts", [])


def _find_bin(*candidates: str) -> str | None:
    """Return the first candidate binary that exists on the system."""
    for name in candidates:
        path = shutil.which(name)
        if path:
            return path
    return None


# ── Core actions ──────────────────────────────────────────────────────────────

def run_getmail(account: dict) -> bool:
    """
    Fetch mail for a single account using getmail6.
    Returns True on success, False on any error.
    """
    name = account["name"]
    conf = GETMAIL_DIR / f"{name}.conf"
    state_dir = GETMAIL_DIR / name

    if not conf.exists():
        log.error("getmail config missing for account %r: %s", name, conf)
        return False

    getmail_bin = _find_bin(
        "/usr/bin/getmail6",
        "/usr/local/bin/getmail6",
        "getmail6",
        "/usr/bin/getmail",
        "getmail",
    )
    if not getmail_bin:
        log.error("[%s] getmail6/getmail binary not found – is getmail6 installed?", name)
        return False

    state_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        getmail_bin,
        f"--getmaildir={state_dir}",
        f"--rcfile={conf}",
    ]

    log.info("Fetching account: %s", name)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )

        if result.stdout.strip():
            log.info("[%s] %s", name, result.stdout.strip())
        if result.stderr.strip():
            log.warning("[%s] stderr: %s", name, result.stderr.strip())

        if result.returncode not in (0, 1):
            # getmail exits 1 when there are no new messages on some backends
            log.error("[%s] getmail6 exited with code %d", name, result.returncode)
            return False

        return True

    except subprocess.TimeoutExpired:
        log.error("[%s] getmail6 timed out after 120 s", name)
        return False
    except Exception as exc:
        log.exception("[%s] unexpected error: %s", name, exc)
        return False


def run_runqueue() -> bool:
    """
    Flush the msmtpq spool directory by calling msmtp-runqueue.
    Returns True if the queue was flushed without errors.
    """
    runqueue_bin = _find_bin(
        "/usr/libexec/msmtp/msmtpq/msmtp-queue",
        "/usr/sbin/msmtp-runqueue",
        "/usr/local/sbin/msmtp-runqueue",
        "msmtp-runqueue",
    )
    if not runqueue_bin:
        log.error("msmtp-runqueue not found – is msmtp-mta installed?")
        return False

    log.info("Running %s…", runqueue_bin)

    try:
        result = subprocess.run(
            [runqueue_bin, "-r"],
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "MSMTPQ_Q": "/var/spool/msmtp"},
        )

        if result.stdout.strip():
            log.info("[runqueue] %s", result.stdout.strip())
        if result.stderr.strip():
            log.warning("[runqueue] stderr: %s", result.stderr.strip())

        return result.returncode == 0

    except FileNotFoundError:
        log.error("/usr/sbin/msmtp-runqueue not found – is msmtp-mta installed?")
        return False
    except subprocess.TimeoutExpired:
        log.error("msmtp-runqueue timed out after 60 s")
        return False
    except Exception as exc:
        log.exception("runqueue error: %s", exc)
        return False


# ── Scheduled job ─────────────────────────────────────────────────────────────

def fetch_all() -> None:
    """
    Fetch mail from all accounts and flush the outbound queue.
    This function is called by the scheduler and may also be triggered manually.
    """
    global _status

    if _status.get("running"):
        log.warning("Fetch already in progress – skipping this run.")
        return

    _status["running"] = True
    _write_status()

    failed_accounts: list[str] = []

    try:
        accounts = _load_accounts()

        if not accounts:
            log.warning("No accounts configured.")
        else:
            for account in accounts:
                ok = run_getmail(account)
                if not ok:
                    failed_accounts.append(account["name"])

        smtp_ok = run_runqueue()
        qs = _queue_size()

        _status.update(
            {
                "last_fetch": _now_iso(),
                "last_error": (
                    f"Failed accounts: {', '.join(failed_accounts)}"
                    if failed_accounts
                    else None
                ),
                "smtp_ok": smtp_ok,
                "queue_size": qs,
                "fetch_count": _status.get("fetch_count", 0) + 1,
            }
        )

    except Exception as exc:
        log.exception("Unexpected error in fetch_all: %s", exc)
        _status["last_error"] = str(exc)

    finally:
        _status["running"] = False
        _write_status()


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    log.info("Mail Gateway Scheduler starting…")

    # Initialise status on disk
    _status["queue_size"] = _queue_size()
    _write_status()

    config = _load_config()
    interval: int = int(config.get("poll_interval", 60))
    log.info("Poll interval: %d s", interval)

    # Run immediately at startup so the first fetch is not delayed
    fetch_all()

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        fetch_all,
        trigger="interval",
        seconds=interval,
        id="fetch_all",
        max_instances=1,
        coalesce=True,
    )

    # Graceful shutdown on SIGTERM / SIGINT
    def _shutdown(signum, frame):  # type: ignore[type-arg]
        log.info("Received signal %d – shutting down scheduler.", signum)
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    scheduler.start()
    log.info("Scheduler started.")

    # Main loop: watch for manual trigger files and keep the process alive
    while True:
        if TRIGGER_FILE.exists():
            try:
                TRIGGER_FILE.unlink()
            except OSError:
                pass
            log.info("Manual fetch triggered via trigger file.")
            fetch_all()

        time.sleep(2)


if __name__ == "__main__":
    main()
