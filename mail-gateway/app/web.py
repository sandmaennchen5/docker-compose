#!/usr/bin/env python3
"""
Web UI and REST API for Mail Gateway.

Provides a Bootstrap 5 dashboard (/) and a JSON REST API (/api/*).
Runs via Gunicorn (started by Supervisor).
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml
from flask import Flask, jsonify, render_template, request

# ── Paths ─────────────────────────────────────────────────────────────────────

CONFIG_FILE = Path("/config/config.yaml")
ACCOUNTS_FILE = Path("/config/accounts.yaml")
STATUS_FILE = Path("/runtime/status/status.json")
TRIGGER_FILE = Path("/runtime/trigger_fetch")
GETMAIL_DIR = Path("/runtime/getmail")
LOG_DIR = Path("/logs")
SPOOL_DIR = Path("/var/spool/msmtp")

VERSION = "1.0.0"
START_TIME = datetime.now(timezone.utc)

# Allowed log file names (security: prevent path traversal)
_ALLOWED_LOGS = {"scheduler.log", "web.log", "msmtp.log"}

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(
            "/logs/web.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        ),
    ],
)
log = logging.getLogger("web")

# ── Flask app ─────────────────────────────────────────────────────────────────

app = Flask(
    __name__,
    template_folder="/opt/mail-gateway/templates",
    static_folder="/opt/mail-gateway/static",
)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _load_status() -> dict:
    """Read status.json written by the scheduler."""
    try:
        if STATUS_FILE.exists():
            return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read status file: %s", exc)
    return {}


def _load_accounts(mask_passwords: bool = True) -> list[dict]:
    """Load accounts.yaml, optionally masking passwords."""
    try:
        data = yaml.safe_load(ACCOUNTS_FILE.read_text(encoding="utf-8")) or {}
        accounts: list[dict] = data.get("accounts", [])
        if mask_passwords:
            return [{**a, "password": "***"} for a in accounts]
        return accounts
    except Exception as exc:
        log.warning("Could not load accounts: %s", exc)
        return []


def _load_config() -> dict:
    try:
        return yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        log.warning("Could not load config: %s", exc)
        return {}


def _queue_files() -> list[str]:
    if not SPOOL_DIR.exists():
        return []
    try:
        return sorted(p.name for p in SPOOL_DIR.iterdir())
    except OSError:
        return []


def _uptime() -> str:
    delta = datetime.now(timezone.utc) - START_TIME
    total = int(delta.total_seconds())
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m {s}s"


def _read_log_tail(name: str, lines: int = 200) -> str:
    """Return the last *lines* lines of a log file."""
    path = LOG_DIR / name
    if not path.exists():
        return ""
    try:
        result = subprocess.run(
            ["tail", "-n", str(lines), str(path)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout
    except Exception:
        return ""


def _trigger_fetch() -> None:
    """Signal the scheduler to run an immediate fetch via trigger file."""
    TRIGGER_FILE.parent.mkdir(parents=True, exist_ok=True)
    TRIGGER_FILE.touch()


# ── REST API ──────────────────────────────────────────────────────────────────

@app.route("/api/status")
def api_status() -> object:
    status = _load_status()
    status["version"] = VERSION
    status["uptime"] = _uptime()
    status["queue_size"] = len(_queue_files())
    return jsonify(status)


@app.route("/api/accounts")
def api_accounts() -> object:
    return jsonify(_load_accounts(mask_passwords=True))


@app.route("/api/logs")
def api_logs() -> object:
    """
    Return the tail of a log file.
    Query parameter: ?file=scheduler.log  (default)
    Only files in the allowed set are served.
    """
    name = request.args.get("file", "scheduler.log")

    # Validate: must be in allowed list OR match getmail-<name>.log pattern
    is_getmail_log = name.startswith("getmail-") and name.endswith(".log") and "/" not in name
    if name not in _ALLOWED_LOGS and not is_getmail_log:
        return jsonify({"error": "File not allowed."}), 403

    content = _read_log_tail(name)
    return jsonify({"file": name, "content": content})


@app.route("/api/queue")
def api_queue() -> object:
    files = _queue_files()
    return jsonify({"files": files, "count": len(files)})


@app.route("/api/fetch", methods=["POST"])
def api_fetch() -> object:
    """Trigger an immediate fetch in the scheduler process via trigger file."""
    try:
        _trigger_fetch()
        return jsonify({"ok": True, "message": "Fetch triggered."})
    except Exception as exc:
        log.exception("api_fetch error")
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/runqueue", methods=["POST"])
def api_runqueue() -> object:
    """Flush the msmtpq spool directory."""
    runqueue_bin = shutil.which("/usr/libexec/msmtp/msmtpq/msmtp-queue") \
        or shutil.which("/usr/sbin/msmtp-runqueue") \
        or shutil.which("msmtp-runqueue")
    if not runqueue_bin:
        return jsonify({"ok": False, "error": "msmtp-queue not found – is msmtp-mta installed?"}), 500
    try:
        result = subprocess.run(
            [runqueue_bin, "-r"],
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "MSMTPQ_Q": "/var/spool/msmtp"},
        )
        return jsonify(
            {
                "ok": result.returncode == 0,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "msmtp-runqueue not found."}), 500
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "msmtp-runqueue timed out."}), 500
    except Exception as exc:
        log.exception("api_runqueue error")
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/reload", methods=["POST"])
def api_reload() -> object:
    """Re-generate runtime configs from config.yaml / accounts.yaml."""
    try:
        result = subprocess.run(
            [sys.executable, "/opt/mail-gateway/app/generator.py"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return jsonify({"ok": False, "error": result.stderr or result.stdout}), 500
        return jsonify({"ok": True, "message": "Configuration reloaded."})
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "generator.py timed out."}), 500
    except Exception as exc:
        log.exception("api_reload error")
        return jsonify({"ok": False, "error": str(exc)}), 500


# ── Health endpoint ───────────────────────────────────────────────────────────

@app.route("/health")
def health() -> object:
    return jsonify({"status": "ok", "version": VERSION})


# ── Web UI ────────────────────────────────────────────────────────────────────

@app.route("/")
def index() -> object:
    status = _load_status()
    status["version"] = VERSION
    status["uptime"] = _uptime()

    queue = _queue_files()
    status["queue_size"] = len(queue)

    accounts = _load_accounts(mask_passwords=True)

    # Build list of available log files
    log_files = sorted(
        [f.name for f in LOG_DIR.glob("*.log")] if LOG_DIR.exists() else []
    )

    return render_template(
        "index.html",
        status=status,
        accounts=accounts,
        queue=queue,
        log_files=log_files,
        version=VERSION,
        uptime=_uptime(),
    )


# ── Entry point (development only – production uses Gunicorn) ─────────────────

if __name__ == "__main__":
    port = 8080
    try:
        cfg = _load_config()
        port = cfg.get("web", {}).get("port", 8080)
    except Exception:
        pass
    log.info("Starting development server on port %d", port)
    app.run(host="0.0.0.0", port=port, debug=False)
