"""
Unified Jackpot Tracker — Flask app.

Drives both Zitobox and CarnivalCiti scrapers from a single dashboard.
Each source has its own background worker thread (hourly cycle, 8h
session cap) and can be force-scrape'd on demand without interrupting
the regular cycle.
"""
from __future__ import annotations

import asyncio
from playwright.async_api import async_playwright
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from flask import Flask, jsonify, render_template, request

import db
from scrapers import SCRAPERS, get_scraper

HERE = Path(__file__).resolve().parent
app = Flask(__name__, template_folder="templates", static_folder="static")
LOG_FILE = HERE / "data" / "tracker.log"

# Per-source state. Each source has its own worker thread and force-
# scrape slot. force_scrape runs in a separate daemon thread so it
# never blocks the hourly cycle.
state_lock = threading.RLock()
MAX_LOG_LINES = 250
MAX_SESSION = timedelta(hours=8)
DEFAULT_INTERVAL = timedelta(hours=1)

sources_state: dict[str, dict] = {
    name: {
        "active": False,
        "interval_seconds": int(DEFAULT_INTERVAL.total_seconds()),
        "start_time": None,
        "last_run": None,
        "last_run_status": None,
        "last_run_duration": None,
        "next_run": None,
        "stop_event": None,
        "thread": None,
        "force_running": False,
        "force_last_result": None,
        "logs": deque(maxlen=MAX_LOG_LINES),
        "cycle_count": 0,
        "cycle_durations": [],   # last N cycle durations (seconds) for adaptive delay
        "avg_cycle_duration": None,  # current moving average
    }
    for name in SCRAPERS
}

# How many recent cycle durations to average for the next-run delay
AVG_WINDOW = 5


def _log(source: str, msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with state_lock:
        sources_state[source]["logs"].append(line)
    # Persistent disk log (append). Never let I/O break the worker.
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        ts_iso = datetime.now().isoformat(timespec="seconds")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{ts_iso}] [{source}] {msg}\n")
    except Exception:
        pass


class _SourceLogRedirect:
    """Capture stdout writes that match `[ISO timestamp] msg` and tee
    them into the per-source log buffer so the GUI shows them too.
    The scraper prints lines like `[2026-06-05T...] Zitobox Scraping...`
    which we re-parse to extract the message body."""

    def __init__(self, source: str):
        self.source = source

    def write(self, s: str) -> None:
        if not s:
            return
        # Pass through to real stdout
        import sys
        try:
            sys.__stdout__.write(s)
            sys.__stdout__.flush()
        except Exception:
            pass
        for line in s.splitlines():
            line = line.strip()
            if not line:
                continue
            # Strip leading ISO timestamp if present (e.g. [2026-06-05T13:25:46.xxx])
            import re
            line = re.sub(r"^\[\d{4}-\d{2}-\d{2}T[\d:.]+\]\s*", "", line)
            # Prefix with timestamp
            ts = datetime.now().strftime("%H:%M:%S")
            with state_lock:
                sources_state[self.source]["logs"].append(f"[{ts}] {line}")

    def flush(self) -> None:
        try:
            sys.__stdout__.flush()
        except Exception:
            pass


def _with_log_redirect(source: str, fn, *args, **kwargs):
    """Run `fn` with stdout redirected so scraper prints show up in the
    per-source log buffer (in addition to the real terminal)."""
    import contextlib
    import sys
    original = sys.stdout
    sys.stdout = _SourceLogRedirect(source)
    try:
        return fn(*args, **kwargs)
    finally:
        sys.stdout = original


def _next_top_of_hour(from_dt: datetime) -> datetime:
    """Return the next top-of-hour after from_dt.

    E.g. 07:35:00 -> 08:00:00, 07:00:00 -> 08:00:00, 08:00:30 -> 09:00:00.
    """
    base = from_dt.replace(minute=0, second=0, microsecond=0)
    if from_dt >= base:
        return base + timedelta(hours=1)
    return base


def _compute_next_run(state: dict) -> None:
    """Schedule the next run to FINISH on the top of the next hour.

    For each source we keep a moving average of the last N cycle
    durations. The next run is scheduled at:
        next_top_of_hour - avg_duration
    so the cycle ends right at the top of the hour, regardless of how
    long it actually takes. Zitobox (~2 min) starts at HH:58; CC
    (~10 min) starts at HH:50; if durations shift, the schedule
    adapts automatically.

    If the cycle took longer than the gap until the next top of hour
    (start_at would be in the past), skip to the FOLLOWING top of
    hour instead of clamping to "now + 2s". Clamping to "now" caused
    a runaway loop: the schedule kept clamping, the post-wait
    recompute kept re-clamping, and the worker fired a new cycle
    every few seconds — i.e. tracking "more than necessary".
    """
    if not state["active"] or not state["last_run"] or not state["start_time"]:
        state["next_run"] = None
        return
    if datetime.now() - state["start_time"] >= MAX_SESSION:
        state["next_run"] = None
        return
    target_finish = _next_top_of_hour(state["last_run"])
    avg = state.get("avg_cycle_duration")
    if avg and avg > 0:
        # Subtract the average cycle duration so the run FINISHES at
        # the top of the hour.
        start_at = target_finish - timedelta(seconds=avg)
        if start_at <= datetime.now():
            # The cycle took longer than the gap until the next top
            # of hour. Skip to the FOLLOWING top of hour so this
            # cycle has a full hour of room — prevents the runaway
            # loop described in the docstring.
            target_finish = target_finish + timedelta(hours=1)
            start_at = target_finish
        state["next_run"] = start_at
    else:
        # No average yet — fall back to top-of-hour for the first cycle.
        state["next_run"] = target_finish


def _source_worker(source: str, stop_event: threading.Event, force_login: bool) -> None:
    scraper = get_scraper(source)
    tid = threading.get_ident()
    _log(source, f"Worker started tid={tid} (force_login={force_login}).")
    start = datetime.now()
    cycle = 0
    while not stop_event.is_set():
        # Re-check at top of every iteration — defensive against stop
        # racing with the cycle body. (Root cause fix for "Zitobox
        # 2nd run does 5 cycles": a stale thread would loop again
        # otherwise.)
        if stop_event.is_set():
            _log(source, f"[tid={tid}] Stop detected at top of loop.")
            break
        if datetime.now() - start >= MAX_SESSION:
            _log(source, "Reached 8h session cap. Stopping.")
            break
        cycle += 1
        cycle_start = datetime.now()
        _log(source, f"[tid={tid}] Cycle #{cycle} starting (force_login={force_login and cycle == 1})...")
        try:
            _with_log_redirect(source, lambda: asyncio.run(scraper.run(force_login=force_login and cycle == 1)))
            with state_lock:
                sources_state[source]["last_run_status"] = "ok"
            _log(source, f"[tid={tid}] Cycle #{cycle} complete.")
        except Exception as e:
            with state_lock:
                sources_state[source]["last_run_status"] = f"error: {e}"
            _log(source, f"[tid={tid}] Cycle #{cycle} ERROR: {e}")
        with state_lock:
            sources_state[source]["last_run"] = datetime.now()
            sources_state[source]["cycle_count"] = cycle
            duration = (datetime.now() - cycle_start).total_seconds()
            sources_state[source]["last_run_duration"] = duration
            # Track the moving average of the last AVG_WINDOW cycles so
            # the next-run time can subtract this delay and finish on
            # the top of the hour.
            durations = sources_state[source]["cycle_durations"]
            durations.append(duration)
            if len(durations) > AVG_WINDOW:
                durations.pop(0)
            sources_state[source]["avg_cycle_duration"] = sum(durations) / len(durations)
            _compute_next_run(sources_state[source])
        # Wait until the next top-of-hour (or stop)
        with state_lock:
            next_run = sources_state[source].get("next_run")
        if next_run is None:
            break
        wait_seconds = max(0, int((next_run - datetime.now()).total_seconds()))
        _log(source, f"[tid={tid}] Next cycle scheduled for {next_run.strftime('%H:%M:%S')} (waiting {wait_seconds}s)")
        for _ in range(wait_seconds):
            if stop_event.is_set():
                break
            if datetime.now() - start >= MAX_SESSION:
                break
            time.sleep(1)
        # After sleep: while-condition re-checks stop_event itself,
        # so we don't need an explicit re-check here.

    with state_lock:
        sources_state[source]["active"] = False
        sources_state[source]["start_time"] = None
        sources_state[source]["stop_event"] = None
        sources_state[source]["next_run"] = None
        sources_state[source]["stopping"] = False
    _log(source, f"[tid={tid}] Worker stopped.")


def _force_scrape_game_worker(source: str, code: str, force_login: bool) -> None:
    scraper = get_scraper(source)
    with state_lock:
        sources_state[source]["force_running"] = True
        sources_state[source]["force_last_result"] = None
    _log(source, f"Force-scrape game started: {code} (force_login={force_login})")
    try:
        result = _with_log_redirect(
            source,
            lambda: _scrape_one_game(source, scraper, code, force_login),
        )
        with state_lock:
            sources_state[source]["force_last_result"] = {
                "at": datetime.now().isoformat(),
                "ok": 1 if result and "value" in result else 0,
                "total": 1,
                "results": [result] if result else [],
            }
        _log(source, f"Force-scrape {code}: {'OK' if result and 'value' in result else 'FAIL'}")
    except Exception as e:
        with state_lock:
            sources_state[source]["force_last_result"] = {"error": str(e), "at": datetime.now().isoformat()}
        _log(source, f"Force-scrape {code} ERROR: {e}")
    finally:
        with state_lock:
            sources_state[source]["force_running"] = False


def _scrape_one_game(source: str, scraper, code: str, force_login: bool):
    """Scrape a single game using a fresh browser context.

    Reuses the login session (storage_state) if valid, forcing switchBalance
    to 'real' before loading the game URL.
    """
    db.init_db()
    return asyncio.run(_scrape_one_game_async(scraper, code, force_login))


async def _scrape_one_game_async(scraper, code: str, force_login: bool):
    """Async inner: open browser, restore session, switch to real, scrape one game."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 900},
            )
            page = await context.new_page()
            if not await scraper._ensure_logged_in(context, page, force=force_login):
                return {"code": code, "error": "login_failed"}
            game = next((g for g in scraper.load_games() if g["code"] == code), None)
            if not game:
                return {"code": code, "error": f"Game {code} not found"}
            result = await scraper._scrape_game(page, game)
            await context.close()
            return result
        finally:
            await browser.close()


def _force_scrape_worker(source: str, force_login: bool) -> None:
    scraper = get_scraper(source)
    with state_lock:
        sources_state[source]["force_running"] = True
        sources_state[source]["force_last_result"] = None
    _log(source, f"Force-scrape started (force_login={force_login}).")
    try:
        results = _with_log_redirect(source, lambda: asyncio.run(scraper.run(force_login=force_login)))
        with state_lock:
            sources_state[source]["force_last_result"] = {
                "at": datetime.now().isoformat(),
                "ok": sum(1 for r in results if "value" in r),
                "total": len(results),
                "results": results,
            }
        _log(source, f"Force-scrape done: {sources_state[source]['force_last_result']['ok']}/{len(results)}")
    except Exception as e:
        with state_lock:
            sources_state[source]["force_last_result"] = {"error": str(e), "at": datetime.now().isoformat()}
        _log(source, f"Force-scrape ERROR: {e}")
    finally:
        with state_lock:
            sources_state[source]["force_running"] = False


# ─── Routes ────────────────────────────────────────────────────────────────


@app.route("/")
def index():
    db.init_db()
    return render_template("index.html", sources=list(SCRAPERS.keys()))


@app.route("/api/sources")
def api_sources():
    out = []
    for name, scraper in SCRAPERS.items():
        with state_lock:
            st = sources_state[name]
            out.append({
                "name": name,
                "display_name": scraper.display_name,
                "description": scraper.description,
                "active": st["active"],
                "interval_seconds": st["interval_seconds"],
                "start_time": st["start_time"].isoformat() if st["start_time"] else None,
                "last_run": st["last_run"].isoformat() if st["last_run"] else None,
                "last_run_status": st["last_run_status"],
                "last_run_duration": st["last_run_duration"],
                "avg_cycle_duration": st.get("avg_cycle_duration"),
                "next_run": st["next_run"].isoformat() if st["next_run"] else None,
                "force_running": st["force_running"],
                "force_last_result": st["force_last_result"],
                "cycle_count": st["cycle_count"],
            })
    return jsonify({"sources": out})


@app.route("/api/status")
def api_status():
    with state_lock:
        per_source = {}
        for name, st in sources_state.items():
            seconds_remaining = 0
            target_time_display = ""
            if st["active"] and st["start_time"]:
                limit = st["start_time"] + MAX_SESSION
                time_left = limit - datetime.now()
                seconds_remaining = max(0, int(time_left.total_seconds()))
                target_time_display = limit.strftime("%Y-%m-%d %H:%M:%S")
            per_source[name] = {
                "active": st["active"],
                "seconds_remaining": seconds_remaining,
                "target_time_display": target_time_display,
                "logs": list(st["logs"])[-80:],
                "last_run_status": st["last_run_status"],
                "last_run": st["last_run"].isoformat() if st["last_run"] else None,
                "next_run": st["next_run"].isoformat() if st["next_run"] else None,
                "force_running": st["force_running"],
            }

    try:
        latest_id = 0
        with db.get_conn() as c:
            cur = c.execute("SELECT MAX(id) FROM jackpot_values")
            r = cur.fetchone()
            latest_id = r[0] if r and r[0] else 0
    except Exception:
        latest_id = 0
    return jsonify({"per_source": per_source, "latest_id": latest_id})


@app.route("/api/games")
def api_games():
    source = request.args.get("source")
    return jsonify({"games": db.list_games(source)})


@app.route("/api/latest")
def api_latest():
    source = request.args.get("source")
    return jsonify({"values": db.get_latest_values(source)})


@app.route("/api/history")
def api_history():
    source = request.args.get("source", "")
    code = request.args.get("code", "")
    if not source or not code:
        return jsonify({"ok": False, "error": "source and code required"}), 400
    return jsonify({
        "ok": True,
        "history": db.get_history(source, code, limit=1000),
        "sparkline": db.get_sparkline(source, code, points=40),
        "winners": db.get_winners(source, code),
    })


@app.route("/api/stats")
def api_stats():
    return jsonify(db.get_stats())


@app.route("/api/start", methods=["POST"])
@app.route("/api/start/<source>", methods=["POST"])
def start_scraper(source: Optional[str] = None):
    if source is None:
        # start all sources
        for name in SCRAPERS:
            _start_one(name, force_login=False)
        return jsonify({"ok": True, "started": "all"})

    if source not in SCRAPERS:
        return jsonify({"ok": False, "error": f"Unknown source: {source}"}), 400
    data = request.get_json(silent=True) or {}
    _start_one(source, force_login=bool(data.get("force_login", False)))
    return jsonify({"ok": True, "started": source})


def _start_one(source: str, force_login: bool) -> None:
    with state_lock:
        st = sources_state[source]
        if st["active"] and not st.get("stopping"):
            return
        # If a previous worker is still draining from a prior Stop,
        # wait for it OUTSIDE the lock to keep the API responsive.
        old_thread = st.get("thread")
        if old_thread and old_thread.is_alive():
            with state_lock:
                st["stopping"] = True
            old_thread.join(timeout=8)
        # Reset per-cycle tracking so a fresh Start begins clean.
        # cycle_durations / avg_cycle_duration used to leak across restarts
        # and produced schedule drift on quick Start-Stop-Start sequences.
        stop_event = threading.Event()
        st["active"] = True
        st["start_time"] = datetime.now()
        st["stop_event"] = stop_event
        st["stopping"] = False
        st["last_run"] = None
        st["last_run_status"] = None
        st["cycle_count"] = 0
        st["cycle_durations"] = []
        st["avg_cycle_duration"] = None
        st["logs"].clear()
        thread = threading.Thread(
            target=_source_worker,
            args=(source, stop_event, force_login),
            daemon=True,
            name=f"worker-{source}",
        )
        st["thread"] = thread
        thread.start()
    _log(source, "Source started.")


def _stop_one(source: str) -> None:
    with state_lock:
        st = sources_state[source]
        if not st["active"]:
            return
        if st["stop_event"]:
            st["stop_event"].set()
            st["stopping"] = True
    # Wait OUTSIDE the lock for the worker thread to actually exit so
    # a subsequent Start can't race with a still-alive worker (which
    # was the root cause of the "Zitobox 2nd run loops 5 times" bug).
    thread = None
    with state_lock:
        thread = sources_state[source].get("thread")
    if thread:
        thread.join(timeout=8)
    _log(source, "Stop completed (thread joined).")


@app.route("/api/stop", methods=["POST"])
@app.route("/api/stop/<source>", methods=["POST"])
def stop_scraper(source: Optional[str] = None):
    if source is None:
        for name in SCRAPERS:
            _stop_one(name)
        return jsonify({"ok": True, "stopped": "all"})

    if source not in SCRAPERS:
        return jsonify({"ok": False, "error": f"Unknown source: {source}"}), 400
    _stop_one(source)
    return jsonify({"ok": True, "stopped": source})


@app.route("/api/force-scrape/<source>/<code>", methods=["POST"])
def force_scrape_game(source: str, code: str):
    """Scrape a single game (source + code), not the whole source."""
    if source not in SCRAPERS:
        return jsonify({"ok": False, "error": f"Unknown source: {source}"}), 400
    data = request.get_json(silent=True) or {}
    force_login = bool(data.get("force_login", False))
    with state_lock:
        if sources_state[source]["force_running"]:
            return jsonify({"ok": False, "error": "Force-scrape already running"}), 400
    thread = threading.Thread(
        target=_force_scrape_game_worker,
        args=(source, code, force_login),
        daemon=True,
        name=f"force-{source}-{code}",
    )
    thread.start()
    return jsonify({"ok": True, "message": f"Force-scrape started for {code} ({source})"})


@app.route("/api/force-scrape/<source>", methods=["POST"])
def force_scrape(source: str):
    if source not in SCRAPERS:
        return jsonify({"ok": False, "error": f"Unknown source: {source}"}), 400
    data = request.get_json(silent=True) or {}
    force_login = bool(data.get("force_login", False))
    with state_lock:
        if sources_state[source]["force_running"]:
            return jsonify({"ok": False, "error": "Force-scrape already running"}), 400
    thread = threading.Thread(
        target=_force_scrape_worker,
        args=(source, force_login),
        daemon=True,
        name=f"force-{source}",
    )
    thread.start()
    return jsonify({"ok": True, "message": f"Force-scrape started for {source}"})


@app.route("/api/clear/<source>", methods=["POST"])
def clear_source(source: str):
    if source not in SCRAPERS:
        return jsonify({"ok": False, "error": f"Unknown source: {source}"}), 400
    deleted = db.clear_source(source)
    _log(source, f"Cleared {deleted} rows.")
    return jsonify({"ok": True, "deleted": deleted})


@app.route("/api/delete-selected", methods=["POST"])
def delete_selected():
    data = request.get_json(silent=True) or {}
    ids = [int(i) for i in data.get("ids", [])]
    deleted = db.delete_value_ids(ids)
    return jsonify({"ok": True, "deleted": deleted})


@app.route("/api/backup", methods=["POST"])
def backup_db():
    try:
        path = db.create_backup()
        return jsonify({"ok": True, "path": path})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    db.init_db()
    # Pre-load games so the UI shows tabs immediately
    for scraper in SCRAPERS.values():
        try:
            scraper.load_games()
        except Exception as e:
            print(f"[init] load_games({scraper.source}) failed: {e}")
    print("=" * 60)
    print("  Unified Jackpot Tracker")
    print("  Dashboard: http://127.0.0.1:5050")
    print("=" * 60)
    app.run(debug=False, port=5050, use_reloader=False, threaded=True)
