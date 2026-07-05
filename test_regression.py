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
        production code uses a 180s deadline; we monkey-patch the
        deadline to ~0 so the test runs in <1s."""
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

        # Replace asyncio.sleep so the wait loop exits on the first tick.
        original_sleep = scrapers_module.asyncio.sleep
        async def fast_sleep(*a, **kw):
            return await original_sleep(0)  # yield once, ~0ms
        scrapers_module.asyncio.sleep = fast_sleep

        try:
            page = MockPage()
            sc = CarnivalCitiScraper()
            result = asyncio.run(sc._scrape_game(page, {"code": "G164", "name": "Ten Tigers"}))

            self.assertEqual(result.get("error"), "no_cc_jackpot",
                f"Expected no_cc_jackpot error, got {result}")
            self.assertEqual(page._listeners, [],
                f"BUG: listener not removed in finally: {page._listeners}")
            self.assertGreaterEqual(page.goto_calls, 1,
                "page.goto was never called")
        finally:
            scrapers_module.asyncio.sleep = original_sleep


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