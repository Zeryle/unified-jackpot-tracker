"""
Regression tests for unified_tracker_v2.

Runs in <5 seconds with no network and no Playwright browser. Verifies
the three original bug fixes still hold:

  T1: _stop_one() joins the worker thread (no zombie Start/Stop cycles)
  T2: CarnivalCitiScraper._scrape_game removes its listener in finally
      (even when page.goto raises)
  T3: _log() appends to data/tracker.log (disk persistence)

Usage:
  python test_regression.py

Exit codes:
  0 = all tests pass
  1 = at least one test failed
"""

import asyncio
import importlib.util
import os
import sys
import threading
import time
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import MagicMock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


# ─── Test scaffolding ───────────────────────────────────────────────────────


class Stub:
    """Anything-goes stub for modules we don't want to import (playwright)."""
    def __getattr__(self, _): return Stub()
    def __call__(self, *a, **kw): return Stub()


def _load_app_with_stubs():
    """Load app.py with playwright async_api stubbed out so it can be
    imported without the browser deps."""
    # Save real module if present
    saved = sys.modules.get("playwright.async_api")
    sys.modules["playwright.async_api"] = Stub()
    try:
        spec = importlib.util.spec_from_file_location("app_under_test", HERE / "app.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        if saved is not None:
            sys.modules["playwright.async_api"] = saved
        else:
            sys.modules.pop("playwright.async_api", None)
    return mod


def _fake_scraper(source):
    """Replace a scraper with a fast async fake that records events."""
    events = []
    class Fake:
        display_name = source
        description = "fake"
        def load_games(self): return [{"code": "G1", "name": "g1", "url": "u", "jackpot_display": ""}]
        async def run(self, force_login=False):
            events.append(("start", threading.get_ident(), time.time()))
            await asyncio.sleep(0.2)
            events.append(("end", threading.get_ident(), time.time()))
    Fake.source = source
    return Fake(), events


# ─── T1: Zombie worker fix ─────────────────────────────────────────────────


class TestNoZombieWorker(unittest.TestCase):
    def test_start_stop_start_no_zombie(self):
        app = _load_app_with_stubs()
        app.SCRAPERS["zitobox"], z_events = _fake_scraper("zitobox")
        app.SCRAPERS["carnivalciti"], c_events = _fake_scraper("carnivalciti")
        app.get_scraper = lambda s: app.SCRAPERS[s]

        # Pattern: Start -> Stop -> Start. Before the fix the second
        # Start saw active=True and returned early, while the original
        # thread zombied through more cycles.
        app._start_one("zitobox", force_login=False)
        time.sleep(0.05)
        app._stop_one("zitobox")
        time.sleep(0.4)
        app._start_one("zitobox", force_login=False)
        time.sleep(0.4)
        app._stop_one("zitobox")
        time.sleep(0.4)

        starts = [e for e in z_events if e[0] == "start"]
        tids = {e[1] for e in starts}

        # We expect: exactly 2 cycle starts (one per Start), 2 distinct
        # thread ids (each Start spawns a new thread), no tid runs
        # more than one cycle (no zombi).
        self.assertEqual(len(starts), 2,
            f"Expected 2 cycle starts, got {len(starts)}")
        self.assertEqual(len(tids), 2,
            f"Expected 2 distinct tids, got {len(tids)}")
        # Each tid should appear exactly once
        from collections import Counter
        per_tid = Counter(e[1] for e in starts)
        for tid, n in per_tid.items():
            self.assertEqual(n, 1,
                f"Thread {tid} ran {n} cycles (zombie!)")

        # And the worker thread should actually be dead after Stop
        st = app.sources_state["zitobox"]
        self.assertFalse(st["thread"].is_alive(),
            "Worker thread still alive after Stop")
        self.assertFalse(st["active"])

# ─── T2: Carnival listener cleanup ──────────────────────────────────────────


class TestCarnivalListenerCleanup(unittest.TestCase):
    def test_listener_removed_on_goto_failure(self):
        """Verifies that the listener registered in `_scrape_game` is
        removed in `finally`, even when `page.goto()` raises — AND that
        the cleanup happens BEFORE the wait deadline expires. The
        production code uses a 90s per-attempt deadline; we monkey-patch
        the deadline + datetime.now so the test runs in <1s.
        """
        sys.modules["playwright.async_api"] = Stub()
        from scrapers import CarnivalCitiScraper
        import scrapers as scrapers_module

        class MockPage:
            def __init__(self):
                self._listeners = []
                self.goto_calls = 0
            def on(self, evt, fn):
                self._listeners.append((evt, fn))
            def remove_listener(self, evt, fn):
                for i, (e, f) in enumerate(self._listeners):
                    if e == evt and f is fn:
                        del self._listeners[i]
                        return
                raise Exception(f"No listener for {evt}")
            async def goto(self, *a, **kw):
                self.goto_calls += 1
                raise Exception("simulated network failure")

        # Speed up sleep
        original_sleep = scrapers_module.asyncio.sleep
        async def fast_sleep(*a, **kw):
            return await original_sleep(0)
        scrapers_module.asyncio.sleep = fast_sleep

        # Speed up time: monkey-patch scrapers.datetime.now() so the 90s
        # per-attempt deadline trips fast. (scrapers.py does
        # `from datetime import datetime, timedelta`, so we patch the
        # names on the module, not the datetime module itself.)
        import datetime as _dt
        real_scrapers_datetime = scrapers_module.datetime
        real_scrapers_timedelta = scrapers_module.timedelta
        offset = [0.0]
        class FakeDatetime:
            @staticmethod
            def now(tz=None):
                return _dt.datetime.now() + _dt.timedelta(seconds=offset[0])
        scrapers_module.datetime = FakeDatetime

        try:
            page = MockPage()
            sc = CarnivalCitiScraper()
            # Run with a background ticker that advances the fake clock
            import threading as _th
            stop_ticker = _th.Event()
            def tick():
                while not stop_ticker.is_set():
                    offset[0] += 120
                    stop_ticker.wait(0.05)
            t = _th.Thread(target=tick, daemon=True)
            t.start()
            try:
                result = asyncio.run(sc._scrape_game(page, {"code": "G164", "name": "Ten Tigers"}))
            finally:
                stop_ticker.set()

            self.assertEqual(result.get("error"), "no_cc_jackpot",
                f"Expected no_cc_jackpot error, got {result}")
            self.assertEqual(page._listeners, [],
                f"BUG: listener not removed in finally: {page._listeners}")
            # The retry logic now tries up to MAX_ATTEMPTS gotos.
            self.assertGreaterEqual(page.goto_calls, 1,
                "page.goto was never called")
        finally:
            scrapers_module.asyncio.sleep = original_sleep
            scrapers_module.datetime = real_scrapers_datetime


# ─── T3: Log file persistence ──────────────────────────────────────────────


class TestLogPersistence(unittest.TestCase):
    def test_log_appended_to_disk(self):
        app = _load_app_with_stubs()

        # Capture existing log size so we only look at NEW lines.
        log_path = app.LOG_FILE
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if log_path.exists():
            initial_size = log_path.stat().st_size
        else:
            initial_size = 0

        # Trigger a few logs.
        app._log("zitobox", "[test] regression: T3 disk persistence check")
        app._log("carnivalciti", "[test] regression: T3 disk persistence check CC")
        time.sleep(0.05)  # let any deferred writes settle

        self.assertTrue(log_path.exists(), "tracker.log was not created")
        new_content = log_path.read_text(encoding="utf-8")
        self.assertIn("[zitobox] [test] regression: T3 disk persistence check",
            new_content, "Zitobox log line missing from disk")
        self.assertIn("[carnivalciti] [test] regression: T3 disk persistence check CC",
            new_content, "CarnivalCiti log line missing from disk")
        self.assertGreater(log_path.stat().st_size, initial_size,
            "log file size did not grow")

    def test_log_seq_monotonic(self):
        app = _load_app_with_stubs()
        # Snapshot current seq, log N things, snapshot again.
        before = app._log_seq
        for i in range(5):
            app._log("zitobox", f"seq monotonic test #{i}")
        after = app._log_seq
        self.assertGreaterEqual(after - before, 5,
            f"_log_seq did not advance by 5: before={before} after={after}")


# ─── T4: Diagnostics endpoint basic shape ───────────────────────────────────


class TestDiagnosticsEndpoint(unittest.TestCase):
    def test_diagnostics_shape(self):
        app = _load_app_with_stubs()
        # Replace scrapers so we can use the real test_client
        app.SCRAPERS["zitobox"], _ = _fake_scraper("zitobox")
        app.SCRAPERS["carnivalciti"], _ = _fake_scraper("carnivalciti")
        app.get_scraper = lambda s: app.SCRAPERS[s]

        client = app.app.test_client()
        r = client.get("/api/diagnostics")
        self.assertEqual(r.status_code, 200)
        d = r.json
        self.assertIn("process", d)
        self.assertIn("sources", d)
        self.assertIn("zitobox", d["sources"])
        self.assertIn("carnivalciti", d["sources"])
        z = d["sources"]["zitobox"]
        for field in ["active", "thread_alive", "stop_event_set", "cycle_count",
                      "successful_cycles", "error_cycles", "success_rate",
                      "avg_cycle_duration", "last_5_durations", "last_run_status"]:
            self.assertIn(field, z, f"missing field {field!r} in diagnostics.zitobox")
        self.assertIn("pid", d["process"])
        self.assertIn("uptime_seconds", d["process"])
        self.assertIn("log_file", d["process"])

    def test_log_tail_returns_delta(self):
        app = _load_app_with_stubs()
        app.SCRAPERS["zitobox"], _ = _fake_scraper("zitobox")
        app.SCRAPERS["carnivalciti"], _ = _fake_scraper("carnivalciti")
        app.get_scraper = lambda s: app.SCRAPERS[s]
        # Clear the buffer so test is deterministic
        for src in app.sources_state.values():
            src["logs"].clear()

        client = app.app.test_client()
        # Snapshot current global seq
        before = app._log_seq
        # Trigger a few logs
        app._log("zitobox", "[test] log-tail delta")
        time.sleep(0.05)
        after = app._log_seq
        self.assertGreater(after, before, "log_seq did not advance")

        # Fetch from `before` (we want only the delta)
        r = client.get(f"/api/log-tail?source=zitobox&after={before}")
        self.assertEqual(r.status_code, 200)
        d = r.json
        self.assertTrue(d["ok"])
        # Should include the new line
        new_seqs = [line["seq"] for line in d["lines"]]
        self.assertGreaterEqual(len(new_seqs), 1,
            f"Expected at least 1 new line, got {len(new_seqs)}")
        self.assertGreater(min(new_seqs), before,
            f"Got seq {min(new_seqs)} <= before {before}")


# ─── Run ────────────────────────────────────────────────────────────────────

# ─── T5: Zitobox token expiry retry ──────────────────────────────────────────


class TestZitoboxTokenRetry(unittest.TestCase):
    """When the cached token is stale, _scrape_game must invalidate
    cache and retry (re-harvest). It must NOT silently fall through to
    a slow harvest on every cycle (root cause of "5x slower 2nd run"
    bug)."""

    def test_cached_token_used_when_valid(self):
        sys.modules["playwright.async_api"] = Stub()
        from scrapers import ZitoboxScraper
        sc = ZitoboxScraper()
        sc._token_cache["G123"] = "tok_abc"
        sc._rgs_code["G123"] = "G243"

        # Patch the underlying fetcher to return a valid payload
        async def fake_status(rgs, tok):
            return (200, '{"jackpots":{"data":[{"jackpotName":"MINI","levels":[{"currentValue":5000,"level":1}]}]}}')
        sc._fetch_jackpot_httpx_status = fake_status

        import asyncio
        result = asyncio.run(sc._scrape_game({"code": "G123", "name": "Test", "url": "x", "jackpot_display": "MINI"}))
        self.assertIn("value", result)
        self.assertEqual(result["value"], 50.0)

    def test_stale_token_invalidates_cache_and_reharvests(self):
        sys.modules["playwright.async_api"] = Stub()
        from scrapers import ZitoboxScraper
        sc = ZitoboxScraper()
        sc._token_cache["G123"] = "tok_stale"
        sc._rgs_code["G123"] = "G243"

        # First call (cached) returns 401 — token stale.
        # Second call (after invalidate) returns 200 — fresh.
        call_count = [0]
        async def fake_status(rgs, tok):
            call_count[0] += 1
            if call_count[0] == 1 and tok == "tok_stale":
                return (401, "")
            return (200, '{"jackpots":{"data":[{"jackpotName":"MINI","levels":[{"currentValue":7000,"level":1}]}]}}')
        sc._fetch_jackpot_httpx_status = fake_status

        # Patch _harvest_token to return a new token
        async def fake_harvest(code, url):
            return "tok_fresh"
        sc._harvest_token = fake_harvest

        import asyncio
        result = asyncio.run(sc._scrape_game({"code": "G123", "name": "Test", "url": "x", "jackpot_display": "MINI"}))
        self.assertIn("value", result)
        self.assertEqual(result["value"], 70.0)
        # Token must have been swapped to the fresh one
        self.assertEqual(sc._token_cache["G123"], "tok_fresh")
        # Stale token was tried at least once, fresh one too
        self.assertGreaterEqual(call_count[0], 2)


# ─── T6: CarnivalCiti got_data + retry flag ──────────────────────────────────


class TestCarnivalRetryFlag(unittest.TestCase):
    """The new retry logic must:
    - Try multiple attempts when first fails
    - Return got_data=True only when jackpot actually captured
    - Return got_data=False (with attempts >= 1) when all fail
    - Remove listener in finally every time
    """

    def test_got_data_false_when_no_listener_fires(self):
        sys.modules["playwright.async_api"] = Stub()
        from scrapers import CarnivalCitiScraper
        import scrapers as scrapers_module
        sc = CarnivalCitiScraper()

        original_sleep = scrapers_module.asyncio.sleep
        async def fast_sleep(*a, **kw): return await original_sleep(0)
        scrapers_module.asyncio.sleep = fast_sleep

        real_dt = scrapers_module.datetime
        offset = [0.0]
        class FakeDatetime:
            @staticmethod
            def now(tz=None):
                import datetime as _dt
                return _dt.datetime.now() + _dt.timedelta(seconds=offset[0])
        scrapers_module.datetime = FakeDatetime

        class MockPage:
            def __init__(self):
                self._listeners = []
                self.goto_calls = 0
            def on(self, evt, fn): self._listeners.append((evt, fn))
            def remove_listener(self, evt, fn):
                for i,(e,f) in enumerate(self._listeners):
                    if e==evt and f is fn: del self._listeners[i]; return
            async def goto(self, *a, **kw):
                self.goto_calls += 1
                raise Exception("net fail")

        import threading as _th
        stop = _th.Event()
        def tick():
            while not stop.is_set():
                offset[0] += 120
                stop.wait(0.05)
        _th.Thread(target=tick, daemon=True).start()

        try:
            page = MockPage()
            result = asyncio.run(sc._scrape_game(page, {"code": "G164", "name": "Ten Tigers"}))
            self.assertFalse(result.get("got_data"))
            self.assertEqual(result.get("error"), "no_cc_jackpot")
            self.assertGreaterEqual(result.get("attempts", 0), 1)
            # Listener removed even after retries
            self.assertEqual(page._listeners, [],
                f"listener leaked: {page._listeners}")
        finally:
            stop.set()
            scrapers_module.asyncio.sleep = original_sleep
            scrapers_module.datetime = real_dt


# ─── T7: /api/export endpoint ────────────────────────────────────────────────


class TestExportEndpoint(unittest.TestCase):
    def test_export_returns_tsv(self):
        app = _load_app_with_stubs()
        client = app.app.test_client()
        # Insert a few rows directly via the db module
        import tempfile, os
        tmpdir = tempfile.mkdtemp()
        import db as db_module
        real_db_name = db_module.DB_NAME
        real_backup_dir = db_module.BACKUP_DIR
        db_module.DB_NAME = type(real_db_name)(tmpdir + "/test.db")
        db_module.BACKUP_DIR = type(real_backup_dir)(tmpdir + "/backups")
        try:
            db_module.init_db()
            db_module.save_jackpot("zitobox", "G1", "Game1", 1234, "MINI", ts="2026-01-01T10:00:00")
            db_module.save_jackpot("zitobox", "G1", "Game1", 5678, "MINI", ts="2026-01-01T11:00:00")
            db_module.save_jackpot("zitobox", "G2", "Game2", 9999, "GRAND", ts="2026-01-01T12:00:00")
            r = client.get("/api/export?source=zitobox")
            self.assertEqual(r.status_code, 200)
            text = r.data.decode()
            lines = text.strip().split("\n")
            # Header + 3 rows
            self.assertEqual(len(lines), 4, f"Expected 4 lines, got {len(lines)}:\n{text}")
            self.assertIn("timestamp\tsource\tgame_code", lines[0])
            self.assertIn("2026-01-01 10:00:00\tzitobox\tG1", lines[1])
        finally:
            db_module.DB_NAME = real_db_name
            db_module.BACKUP_DIR = real_backup_dir

    def test_export_filters_by_value_range(self):
        app = _load_app_with_stubs()
        client = app.app.test_client()
        import tempfile
        import db as db_module
        tmpdir = tempfile.mkdtemp()
        real_db_name = db_module.DB_NAME
        real_backup_dir = db_module.BACKUP_DIR
        db_module.DB_NAME = type(real_db_name)(tmpdir + "/test2.db")
        db_module.BACKUP_DIR = type(real_backup_dir)(tmpdir + "/backups2")
        try:
            db_module.init_db()
            db_module.save_jackpot("zitobox", "G1", "Game1", 100, "MINI", ts="2026-01-01T10:00:00")
            db_module.save_jackpot("zitobox", "G1", "Game1", 500, "MINI", ts="2026-01-01T11:00:00")
            db_module.save_jackpot("zitobox", "G1", "Game1", 900, "MINI", ts="2026-01-01T12:00:00")
            r = client.get("/api/export?source=zitobox&min_value=200&max_value=800")
            self.assertEqual(r.status_code, 200)
            text = r.data.decode()
            lines = [l for l in text.strip().split("\n") if l]
            self.assertEqual(len(lines), 2, f"Expected header + 1 row (500), got {len(lines)}:\n{text}")
            self.assertIn("\t500", lines[1])
        finally:
            db_module.DB_NAME = real_db_name
            db_module.BACKUP_DIR = real_backup_dir

    def test_export_rejects_unknown_source(self):
        app = _load_app_with_stubs()
        client = app.app.test_client()
        r = client.get("/api/export?source=bitcoin")
        self.assertEqual(r.status_code, 400)


# ─── T8: /api/history filters ────────────────────────────────────────────────


class TestHistoryFilters(unittest.TestCase):
    def test_history_supports_value_range(self):
        app = _load_app_with_stubs()
        client = app.app.test_client()
        import tempfile
        import db as db_module
        tmpdir = tempfile.mkdtemp()
        real_db_name = db_module.DB_NAME
        real_backup_dir = db_module.BACKUP_DIR
        db_module.DB_NAME = type(real_db_name)(tmpdir + "/test3.db")
        db_module.BACKUP_DIR = type(real_backup_dir)(tmpdir + "/backups3")
        try:
            db_module.init_db()
            db_module.save_jackpot("zitobox", "G1", "Game1", 100, "MINI", ts="2026-01-01T10:00:00")
            db_module.save_jackpot("zitobox", "G1", "Game1", 500, "MINI", ts="2026-01-01T11:00:00")
            db_module.save_jackpot("zitobox", "G1", "Game1", 900, "MINI", ts="2026-01-01T12:00:00")
            r = client.get("/api/history?source=zitobox&code=G1&min_value=200&max_value=800")
            self.assertEqual(r.status_code, 200)
            d = r.json
            self.assertTrue(d["ok"])
            self.assertEqual(len(d["history"]), 1)
            self.assertEqual(d["history"][0]["value"], 500.0)
        finally:
            db_module.DB_NAME = real_db_name
            db_module.BACKUP_DIR = real_backup_dir


# ─── Run ────────────────────────────────────────────────────────────────────


def main():
    # Force minimal output: one line per test.
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(__import__(__name__))
    runner = unittest.TextTestRunner(verbosity=2, stream=sys.stderr)
    result = runner.run(suite)
    print()
    if result.wasSuccessful():
        print("=" * 60)
        print(f"  ✓ All {result.testsRun} regression tests passed.")
        print("=" * 60)
        sys.exit(0)
    else:
        print("=" * 60)
        print(f"  ✗ {len(result.failures)} failures, {len(result.errors)} errors")
        print("=" * 60)
        sys.exit(1)


if __name__ == "__main__":
    main()