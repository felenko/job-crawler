"""
Dynamic jobs browser server.

- Serves a single-page app that fetches job data from /api/jobs (live from Jobs folder).
- Refresh in the UI or reload the page to see current state (scraper can run in parallel).
- Buttons: Rescrape all, Rescrape errors only, Rescrape empty only — start the crawler in background.

Run from WebCrawler directory:
  python jobs_server.py
  python jobs_server.py --port 5000

Then open http://localhost:5000
"""
from __future__ import annotations

import argparse
import atexit
import json
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

# Import data collection and crawler's company-name logic
from build_jobs_browser import (
    MARKER_EMPTY,
    MARKER_ERROR,
    MARKER_SCAN,
    collect_browser_data,
)
from jobs_db import (compute_hash, get_applied_hashes, init_db, mark_applied, unmark_applied,
                     get_rejected_hashes, mark_rejected, unmark_rejected)

app = Flask(__name__, static_folder="jobs_browser", static_url_path="")

# Paths relative to WebCrawler (where this script lives)
BASE = Path(__file__).resolve().parent
LOG_FILE = BASE / "jobs_server.log"
JOBS_DIR = BASE / "Jobs"
SEEDS_FILE = BASE / "seeds_test.txt"
CRAWLER_SCRIPT = BASE / "job_crawler.py"
PROGRESS_FILE = JOBS_DIR / ".scrape_progress"  # bulk operations

# Unique ID for this server process lifetime.  Written into every progress file
# so that on restart we can recognise files from the previous session as stale —
# regardless of whether the OS reused the crawler's PID.
SERVER_SESSION = str(uuid.uuid4())


def _company_progress_file(company: str) -> Path:
    """Per-company progress file so multiple single-company scrapes can run in parallel."""
    safe = re.sub(r"[^\w.-]", "_", company)[:64]
    return JOBS_DIR / f".scrape_progress_{safe}"



def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _parse_seeds_file() -> list[tuple[str, str]]:
    """Parse seeds_test.txt. Returns list of (company_name, url). Lines without comma: company is empty, we use url only."""
    if not SEEDS_FILE.is_file():
        return []
    out = []
    for line in SEEDS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "," in line:
            company_part, url_part = line.split(",", 1)
            company_part = company_part.strip()
            url_part = url_part.strip()
            if url_part:
                out.append((company_part, url_part))
        else:
            out.append(("", line))
    return out


def _get_company_seed_url_from_seeds_file(company: str) -> str | None:
    """Get the seed URL for a company from seeds_test.txt. Match by company name (line format: CompanyName, URL)."""
    for name, url in _parse_seeds_file():
        if name and name.lower() == company.lower():
            return url
    return None


def _get_company_seed_url(company: str) -> str | None:
    """Get the seed URL for a company: first from seeds_test.txt, then from marker files."""
    # Prefer seeds file so editing seeds_test.txt and pressing Rescrape uses the new URL
    url = _get_company_seed_url_from_seeds_file(company)
    if url:
        return url
    company_dir = JOBS_DIR / company
    if not company_dir.is_dir():
        return None
    for marker_name in (MARKER_SCAN, MARKER_EMPTY, MARKER_ERROR):
        data = _read_json(company_dir / marker_name)
        if data and data.get("url"):
            return data["url"].strip()
    return None


def _get_seed_entries_for_filter(filter_kind: str) -> list[tuple[str, str]]:
    """Return list of (company_name, url) pairs for 'all', 'errors', or 'empty'."""
    if filter_kind == "all":
        # Preserve company names exactly as written in seeds file
        return [(name, url) for name, url in _parse_seeds_file()]

    if filter_kind == "errors":
        marker_name = MARKER_ERROR
    elif filter_kind == "empty":
        marker_name = MARKER_EMPTY
    else:
        return []

    entries = []
    if not JOBS_DIR.is_dir():
        return entries
    for company_dir in sorted(JOBS_DIR.iterdir()):
        if not company_dir.is_dir() or company_dir.name.startswith("."):
            continue
        marker_path = company_dir / marker_name
        if not marker_path.is_file():
            continue
        data = _read_json(marker_path)
        if data and data.get("url"):
            # Use the folder name as company name so it matches the existing directory
            entries.append((company_dir.name, data["url"].strip()))
    return entries


def _start_rescrape(filter_kind: str) -> tuple[bool, str]:
    """Start crawler in background. Returns (ok, message)."""
    entries = _get_seed_entries_for_filter(filter_kind)
    if not entries:
        return False, f"No URLs to scrape for filter '{filter_kind}'."
    return _start_rescrape_entries(entries, f"filter={filter_kind} ({len(entries)} URLs)")


def _start_rescrape_company(company: str) -> tuple[bool, str]:
    """Start crawler for a single company with its own progress file (supports parallel scrapes)."""
    url = _get_company_seed_url(company)
    if not url:
        return False, f"No seed URL found for company '{company}' (no marker file with url)."
    return _start_rescrape_entries([(company, url)], f"company {company}",
                                   progress_file=_company_progress_file(company))


def _start_rescrape_entries(entries: list[tuple[str, str]], label: str,
                            progress_file: Path | None = None) -> tuple[bool, str]:
    """Start crawler with given (company, url) entries. Writes 'CompanyName, URL' seed file."""
    if not entries:
        return False, "No URLs to scrape."
    if not CRAWLER_SCRIPT.is_file():
        return False, "job_crawler.py not found."
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".txt",
            delete=False,
            encoding="utf-8",
        ) as f:
            for company, url in entries:
                # Write in 'CompanyName, URL' format so the crawler uses the exact
                # folder name — prevents mismatch between folder and URL-derived name
                if company:
                    f.write(f"{company}, {url}\n")
                else:
                    f.write(url + "\n")
            seed_path = f.name
    except Exception as e:
        return False, str(e)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    pf = progress_file if progress_file is not None else PROGRESS_FILE
    # Write session stamp so on the next server restart we can identify this
    # file as stale (regardless of PID reuse) and clean it up automatically.
    try:
        pf.write_text(f"session:{SERVER_SESSION}\n", encoding="utf-8")
    except Exception:
        pass
    progress_path = str(pf)
    cmd = [
        sys.executable,
        str(CRAWLER_SCRIPT),
        seed_path,
        "--output",
        str(JOBS_DIR),
        "--fresh-hours",
        "0",
        "--retry-empty-hours",
        "0",
        "--retry-error-hours",
        "0",
        "--progress-file",
        progress_path,
    ]
    # On Windows, CREATE_NO_WINDOW fully detaches the child from the parent
    # console so Playwright/Chromium starting up can't send Ctrl+C (SIGINT)
    # back to the Flask server process.  On non-Windows, start_new_session
    # achieves the same isolation.
    _popen_kw: dict = {
        "cwd": str(BASE),
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        _popen_kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    else:
        _popen_kw["start_new_session"] = True
    try:
        subprocess.Popen(cmd, **_popen_kw)
    except FileNotFoundError:
        return False, "Python or job_crawler.py not found."
    except Exception as e:
        return False, str(e)
    return True, f"Scraping started for {label}. Watch progress below."


# Keep old name as alias so nothing external breaks
_start_rescrape_urls = _start_rescrape_entries


@app.route("/api/jobs")
def api_jobs():
    """Return current job data by scanning Jobs folder, annotated with applied state."""
    data = collect_browser_data(JOBS_DIR)
    applied = get_applied_hashes()
    rejected = get_rejected_hashes()
    for company_data in data:
        for job in company_data['jobs']:
            h = compute_hash(company_data['company'], job['title'])
            job['job_hash'] = h
            job['applied'] = h in applied
            job['rejected'] = h in rejected
    return jsonify(data)


@app.route("/api/seeds")
def api_seeds():
    """Return all companies from seeds_test.txt as [{name, url}]."""
    return jsonify([{"name": name, "url": url} for name, url in _parse_seeds_file()])


@app.route("/api/applied", methods=["POST", "DELETE"])
def api_applied():
    """Toggle applied state. POST to mark, DELETE to unmark. Body: { job_hash, company, title, url }."""
    body = request.get_json(silent=True) or {}
    job_hash = (body.get("job_hash") or "").strip()
    if not job_hash:
        return jsonify({"ok": False, "message": "job_hash required"}), 400
    if request.method == "POST":
        mark_applied(
            job_hash,
            (body.get("company") or "").strip(),
            (body.get("title") or "").strip(),
            (body.get("url") or "").strip(),
        )
    else:
        unmark_applied(job_hash)
    return jsonify({"ok": True})


@app.route("/api/rejected", methods=["POST", "DELETE"])
def api_rejected():
    """Toggle rejected state. POST to mark, DELETE to unmark. Body: { job_hash, company, title, url }."""
    body = request.get_json(silent=True) or {}
    job_hash = (body.get("job_hash") or "").strip()
    if not job_hash:
        return jsonify({"ok": False, "message": "job_hash required"}), 400
    if request.method == "POST":
        mark_rejected(
            job_hash,
            (body.get("company") or "").strip(),
            (body.get("title") or "").strip(),
            (body.get("url") or "").strip(),
        )
    else:
        unmark_rejected(job_hash)
    return jsonify({"ok": True})


def _is_process_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


@app.route("/api/progress")
def api_progress():
    """Return scrape progress. ?job=CompanyName for a single company; no param for bulk."""
    out = {"running": False, "lines": [], "summary": None}
    job = request.args.get("job", "").strip()
    pf = _company_progress_file(job) if job else PROGRESS_FILE
    if not pf.is_file():
        return jsonify(out)
    try:
        raw = pf.read_text(encoding="utf-8")
    except Exception:
        return jsonify(out)
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not lines:
        return jsonify(out)
    pid = None
    if lines[0].startswith("pid:"):
        try:
            pid = int(lines[0].split(":", 1)[1])
        except (IndexError, ValueError):
            pass
    # Check for the exact "done" line — not just the substring,
    # which would false-match "company_done:..." lines.
    finished = any(ln.strip() == "done" for ln in lines)
    out["running"] = pid is not None and _is_process_running(pid) and not finished
    out["lines"] = lines
    if finished and not out["running"]:
        out["summary"] = "Done. Refresh the page to see updated jobs."
    return jsonify(out)


def _progress_file_is_active(pf: Path) -> bool:
    """Return True only if this progress file belongs to the current server session
    AND the crawler process is still running.

    Session-stamp check eliminates false positives from OS PID reuse: any file
    written by a previous server instance will have a different session token and
    is unconditionally treated as stale.
    """
    try:
        raw = pf.read_text(encoding="utf-8")
    except Exception:
        return False
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not lines:
        return False
    if any(ln == "done" for ln in lines):
        return False
    # Session check — must match this server instance
    session_line = next((ln for ln in lines if ln.startswith("session:")), None)
    if session_line and session_line.split(":", 1)[1] != SERVER_SESSION:
        return False  # stale: belongs to a previous server run
    # PID check as secondary guard (handles cases where session line is absent)
    pid = None
    for ln in lines:
        if ln.startswith("pid:"):
            try:
                pid = int(ln.split(":", 1)[1])
            except (IndexError, ValueError):
                pass
            break
    if pid is not None and not _is_process_running(pid):
        return False
    # If we have a valid session match, trust it even without a pid yet
    return session_line is not None


def cleanup_stale_progress_files() -> None:
    """On server startup, delete any progress files that don't belong to this session.

    Because SERVER_SESSION is a fresh UUID on every startup, ALL files from
    previous runs are stale — regardless of whether the OS reused their PIDs.
    """
    if not JOBS_DIR.is_dir():
        return
    targets = list(JOBS_DIR.glob(".scrape_progress*"))
    if PROGRESS_FILE.is_file():
        targets.append(PROGRESS_FILE)
    for f in targets:
        if not f.is_file():
            continue
        if not _progress_file_is_active(f):
            try:
                f.unlink()
            except Exception:
                pass


@app.route("/api/progress/active")
def api_progress_active():
    """Return list of company names that currently have a per-company scrape in progress."""
    active = []
    if not JOBS_DIR.is_dir():
        return jsonify({"active": active})
    prefix = ".scrape_progress_"
    for f in JOBS_DIR.iterdir():
        if not f.name.startswith(prefix) or not f.is_file():
            continue
        company = f.name[len(prefix):]
        if not company:
            continue
        if _progress_file_is_active(f):
            active.append(company)
    return jsonify({"active": active})


@app.route("/api/progress", methods=["DELETE"])
def api_progress_clear():
    """Force-clear a stuck progress file. Body: { "job": "CompanyName" } or empty for bulk."""
    body = request.get_json(silent=True) or {}
    job = (body.get("job") or "").strip()
    pf = _company_progress_file(job) if job else PROGRESS_FILE
    try:
        if pf.is_file():
            pf.unlink()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


@app.route("/api/rescrape", methods=["POST"])
def api_rescrape():
    """Start rescrape. Body: { \"filter\": \"all\"|\"errors\"|\"empty\" } or { \"company\": \"CompanyName\" }."""
    body = request.get_json(silent=True) or {}
    company = (body.get("company") or "").strip()
    if company:
        ok, message = _start_rescrape_company(company)
    else:
        filter_kind = (body.get("filter") or "all").strip().lower()
        if filter_kind not in ("all", "errors", "empty"):
            return jsonify({"ok": False, "message": "filter must be all, errors, or empty"}), 400
        ok, message = _start_rescrape(filter_kind)
    if not ok:
        return jsonify({"ok": False, "message": message}), 400
    return jsonify({"ok": True, "message": message})


@app.route("/")
def index():
    """Serve the dynamic jobs browser page."""
    return send_from_directory(app.static_folder, "index.html")


def _setup_logging() -> logging.Logger:
    log = logging.getLogger("jobs_server")
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s")
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    log.addHandler(ch)
    try:
        fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except Exception:
        pass
    return log


def main():
    init_db()
    cleanup_stale_progress_files()
    ap = argparse.ArgumentParser(description="Run dynamic jobs browser server")
    ap.add_argument("--port", "-p", type=int, default=5000, help="Port to bind")
    ap.add_argument("--debug", action="store_true", help="Flask debug mode")
    args = ap.parse_args()

    log = _setup_logging()

    # Log every possible exit path so we know why the process stopped
    atexit.register(lambda: log.info("Process exiting (atexit)."))

    def _sig(signum, _frame):
        log.info("Received signal %s — shutting down.", signum)
        sys.exit(0)

    for _s in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(_s, _sig)
        except (OSError, ValueError):
            pass
    # Windows-only: fired when the console window is closed
    if hasattr(signal, "SIGBREAK"):
        try:
            signal.signal(signal.SIGBREAK, _sig)
        except (OSError, ValueError):
            pass

    url = f"http://localhost:{args.port}"
    print()
    print("  Jobs data from:", JOBS_DIR.resolve())
    print("  Open in your browser:", url)
    print("  (Do NOT open index.html as a file — use the URL above.)")
    print(f"  Log file: {LOG_FILE}")
    print()

    restart_delay = 3
    while True:
        try:
            log.info("Server starting on %s", url)
            app.run(host="127.0.0.1", port=args.port, debug=args.debug, use_reloader=False)
            log.info("app.run() returned normally — exiting.")
            break
        except (KeyboardInterrupt, SystemExit):
            log.info("Stopped (KeyboardInterrupt/SystemExit).")
            break
        except OSError as exc:
            msg = str(exc).lower()
            if "address already in use" in msg or "only one usage" in msg:
                log.error("Port %d already in use — is another instance running?", args.port)
                break
            log.error("OSError: %s\n%s", exc, traceback.format_exc())
            log.info("Restarting in %ds…", restart_delay)
            time.sleep(restart_delay)
        except Exception:
            log.error("Unexpected crash:\n%s", traceback.format_exc())
            log.info("Restarting in %ds…", restart_delay)
            time.sleep(restart_delay)


if __name__ == "__main__":
    main()
